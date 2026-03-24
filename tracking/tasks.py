from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from botapp.telegram_api import send_message
from common.time import compute_next_run_at
from tracking.models import (
    Competitor,
    UserCompetitor,
    JobRun,
    JobStatus,
    Platform,
    Report,
    ReportStatus,
    Schedule,
    TgUser,
)
from tracking.services.collector import refresh_competitor
from tracking.services.reporting import build_report_payload, render_report_text
from tracking.services.scoring import compute_competitor_baseline, score_items_for_period

logger = logging.getLogger(__name__)


def _generate_and_send_report(*, user: TgUser, period_start, period_end) -> Report:
    max_competitors = int(getattr(settings, "MAX_COMPETITORS_YOUTUBE", 20))
    links = (
        UserCompetitor.objects.select_related("competitor")
        .filter(user=user, is_active=True, competitor__platform=Platform.YOUTUBE)
        .order_by("id")
        .all()[:max_competitors]
    )
    competitors = [lnk.competitor for lnk in links]

    updated_items = []
    for comp in competitors:
        updated_items.extend(
            refresh_competitor(
                competitor=comp,
                mode="incremental",
                captured_at=period_end,
            )
        )

    competitor_by_item_id = {it.id: it.competitor for it in updated_items}
    baseline_by_competitor_id = {}
    for comp in competitors:
        baseline_by_competitor_id[comp.id] = compute_competitor_baseline(competitor=comp, now=period_end)

    scored = score_items_for_period(
        items=updated_items,
        competitor_by_item_id=competitor_by_item_id,
        baseline_by_competitor_id=baseline_by_competitor_id,
        period_start=period_start,
        period_end=period_end,
    )

    payload = build_report_payload(scored=scored, period_start=period_start, period_end=period_end)
    report = Report.objects.create(
        user=user,
        period_start=period_start,
        period_end=period_end,
        status=ReportStatus.CREATED,
        payload=payload,
    )

    text = render_report_text(payload=payload, timezone_str=user.timezone_str)
    send_message(chat_id=int(user.tg_chat_id), text=text)

    report.status = ReportStatus.SENT
    report.sent_at = timezone.now()
    report.save(update_fields=["status", "sent_at"])
    return report


@shared_task
def tick_due_schedules() -> int:
    """
    Finds due schedules and enqueues report jobs.

    To avoid duplicate enqueues, we advance next_run_at before sending the job.
    """
    now = timezone.now()
    # Safety: if a worker crashed mid-job, we can get "stuck" schedules. Clear stale locks.
    stale_minutes = int(getattr(settings, "SCHEDULE_RUNNING_STALE_MINUTES", 60))
    if stale_minutes > 0:
        cutoff = now - timedelta(minutes=stale_minutes)
        Schedule.objects.filter(is_running=True, running_started_at__lt=cutoff).update(
            is_running=False,
            running_started_at=None,
            updated_at=now,
        )

    due = (
        Schedule.objects.select_related("user")
        .filter(is_enabled=True, is_running=False)
        .filter(next_run_at__lte=now)
    )
    enqueued = 0
    for sched in due:
        # Move next_run_at forward to prevent enqueuing again on the next tick.
        try:
            sched.next_run_at = compute_next_run_at(sched.user.timezone_str, list(sched.times or []), now)
            sched.save(update_fields=["next_run_at", "updated_at"])
        except Exception:
            logger.exception("Failed to advance schedule next_run_at (schedule_id=%s)", sched.id)
            continue
        run_user_report.delay(sched.user_id)
        enqueued += 1
    return enqueued


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def run_user_report(self, user_id: int) -> None:
    now = timezone.now()
    user = TgUser.objects.get(id=user_id)

    # Use a transaction for schedule updates to keep period boundaries consistent.
    with transaction.atomic():
        schedule = Schedule.objects.select_for_update().get(user=user)
        if schedule.is_running:
            logger.info("Skipping scheduled report: already running (user_id=%s)", user_id)
            return

        schedule.is_running = True
        schedule.running_started_at = now
        schedule.save(update_fields=["is_running", "running_started_at", "updated_at"])

        if schedule.last_run_at is not None and schedule.last_run_at < now:
            period_start = schedule.last_run_at
        else:
            period_start = now - timedelta(hours=24)
        period_end = now

        job = JobRun.objects.create(
            job_type="run_user_report",
            user=user,
            status=JobStatus.RUNNING,
            started_at=now,
            attempts=int(getattr(self.request, "retries", 0)) + 1,
            payload={"period_start": period_start.isoformat(), "period_end": period_end.isoformat()},
        )

    report: Report | None = None
    try:
        report = _generate_and_send_report(user=user, period_start=period_start, period_end=period_end)

        with transaction.atomic():
            schedule = Schedule.objects.select_for_update().get(user=user)
            # Never move last_run_at backwards (can happen if two jobs finish out of order).
            if schedule.last_run_at is None or period_end > schedule.last_run_at:
                schedule.last_run_at = period_end
            schedule.next_run_at = compute_next_run_at(user.timezone_str, list(schedule.times or []), period_end)
            schedule.is_running = False
            schedule.running_started_at = None
            schedule.save(
                update_fields=["last_run_at", "next_run_at", "is_running", "running_started_at", "updated_at"]
            )

        job.status = JobStatus.SUCCESS
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at"])
    except Exception as e:
        logger.exception("run_user_report failed (user_id=%s)", user_id)

        if report is not None:
            report.status = ReportStatus.FAILED
            report.save(update_fields=["status"])

        try:
            with transaction.atomic():
                schedule = Schedule.objects.select_for_update().get(user=user)
                schedule.is_running = False
                schedule.running_started_at = None
                schedule.next_run_at = timezone.now() + timedelta(minutes=5)
                schedule.save(update_fields=["is_running", "running_started_at", "next_run_at", "updated_at"])
        except Exception:
            logger.exception("Failed to reschedule after error (user_id=%s)", user_id)

        job.status = JobStatus.FAILED
        job.error = str(e)
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error", "finished_at"])

        raise


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def run_user_report_now(self, user_id: int) -> None:
    """
    Manual report trigger ("Отчет сейчас").

    Differences from scheduled runs:
    - Advances next_run_at before running, to avoid an immediate duplicate scheduled report.
    - Uses a minimum window (24h) if last_run_at is too recent.
    """
    now = timezone.now()
    user = TgUser.objects.get(id=user_id)

    with transaction.atomic():
        schedule = Schedule.objects.select_for_update().get(user=user)
        if schedule.is_running:
            logger.info("Skipping manual report: already running (user_id=%s)", user_id)
            return

        schedule.is_running = True
        schedule.running_started_at = now

        if schedule.last_run_at is not None and schedule.last_run_at < now:
            period_start = schedule.last_run_at
        else:
            period_start = now - timedelta(hours=24)
        period_end = now

        # Prevent beat from enqueuing an immediate duplicate run.
        schedule.next_run_at = compute_next_run_at(user.timezone_str, list(schedule.times or []), now)
        schedule.save(update_fields=["is_running", "running_started_at", "next_run_at", "updated_at"])

        job = JobRun.objects.create(
            job_type="run_user_report_now",
            user=user,
            status=JobStatus.RUNNING,
            started_at=now,
            attempts=int(getattr(self.request, "retries", 0)) + 1,
            payload={"period_start": period_start.isoformat(), "period_end": period_end.isoformat()},
        )

    report: Report | None = None
    try:
        report = _generate_and_send_report(user=user, period_start=period_start, period_end=period_end)

        with transaction.atomic():
            schedule = Schedule.objects.select_for_update().get(user=user)
            if schedule.last_run_at is None or period_end > schedule.last_run_at:
                schedule.last_run_at = period_end
            schedule.next_run_at = compute_next_run_at(user.timezone_str, list(schedule.times or []), period_end)
            schedule.is_running = False
            schedule.running_started_at = None
            schedule.save(
                update_fields=["last_run_at", "next_run_at", "is_running", "running_started_at", "updated_at"]
            )

        job.status = JobStatus.SUCCESS
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at"])
    except Exception as e:
        logger.exception("run_user_report_now failed (user_id=%s)", user_id)

        if report is not None:
            report.status = ReportStatus.FAILED
            report.save(update_fields=["status"])

        try:
            with transaction.atomic():
                schedule = Schedule.objects.select_for_update().get(user=user)
                schedule.is_running = False
                schedule.running_started_at = None
                schedule.next_run_at = timezone.now() + timedelta(minutes=5)
                schedule.save(update_fields=["is_running", "running_started_at", "next_run_at", "updated_at"])
        except Exception:
            logger.exception("Failed to reschedule after error (user_id=%s)", user_id)

        job.status = JobStatus.FAILED
        job.error = str(e)
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error", "finished_at"])

        raise


@shared_task
def bootstrap_user_data(user_id: int) -> None:
    """
    Initial data collection after onboarding.

    Purpose: create the first MetricSnapshot so the next scheduled report can compute deltas.
    """
    now = timezone.now()
    user = TgUser.objects.get(id=user_id)
    job = JobRun.objects.create(
        job_type="bootstrap_user_data",
        user=user,
        status=JobStatus.RUNNING,
        started_at=now,
        attempts=1,
        payload={},
    )
    try:
        max_competitors = int(getattr(settings, "MAX_COMPETITORS_YOUTUBE", 20))
        links = (
            UserCompetitor.objects.select_related("competitor")
            .filter(user=user, is_active=True, competitor__platform=Platform.YOUTUBE)
            .order_by("id")
            .all()[:max_competitors]
        )
        competitors = [lnk.competitor for lnk in links]
        for comp in competitors:
            refresh_competitor(competitor=comp, mode="incremental", captured_at=now)

        # Mark the baseline point for the first delta window.
        with transaction.atomic():
            schedule = Schedule.objects.select_for_update().get(user=user)
            if schedule.last_run_at is None:
                schedule.last_run_at = now
                schedule.save(update_fields=["last_run_at", "updated_at"])

        job.status = JobStatus.SUCCESS
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at"])
    except Exception as e:
        logger.exception("bootstrap_user_data failed (user_id=%s)", user_id)
        job.status = JobStatus.FAILED
        job.error = str(e)
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error", "finished_at"])
        raise
