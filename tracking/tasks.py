from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from botapp.telegram_api import send_message
from common.time import compute_next_run_at, format_dt_local
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
from tracking.services.collector import refresh_youtube_competitor
from tracking.services.reporting import build_report_payload, render_report_text
from tracking.services.scoring import compute_competitor_baseline, score_items_for_period

logger = logging.getLogger(__name__)


def _get_active_competitors(*, user: TgUser) -> list[Competitor]:
    max_competitors = int(
        getattr(settings, "MAX_COMPETITORS_PER_PLATFORM", getattr(settings, "MAX_COMPETITORS_YOUTUBE", 20))
    )
    competitors: list[Competitor] = []
    for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM):
        links = (
            UserCompetitor.objects.select_related("competitor")
            .filter(user=user, is_active=True, competitor__platform=platform)
            .order_by("id")
            .all()[:max_competitors]
        )
        competitors.extend(link.competitor for link in links)
    return competitors


def _dt_iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _parse_due_at(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _dt_local(value: datetime | None, timezone_str: str) -> str:
    if value is None:
        return ""
    return format_dt_local(value, timezone_str)


def _compute_bootstrap_last_run_at(*, schedule: Schedule, captured_at: datetime) -> datetime:
    if schedule.next_run_at is None:
        return captured_at
    return min(captured_at, schedule.next_run_at - timedelta(seconds=1))


def _generate_and_send_report(*, user: TgUser, period_start, period_end) -> Report:
    max_competitors = int(getattr(settings, "MAX_COMPETITORS_YOUTUBE", 100))
    links = (
        UserCompetitor.objects.select_related("competitor")
        .filter(user=user, is_active=True, competitor__platform=Platform.YOUTUBE)
        .order_by("id")
        .all()[:max_competitors]
    )
    competitors = [lnk.competitor for lnk in links]
    logger.info(
        "report_collect_start user_id=%s tg_user_id=%s competitors=%s period_start=%s period_end=%s",
        user.id,
        user.tg_user_id,
        len(competitors),
        _dt_iso(period_start),
        _dt_iso(period_end),
    )

    updated_items = []
    for comp in competitors:
        try:
            updated_items.extend(
                refresh_youtube_competitor(
                    competitor=comp,
                    mode="incremental",
                    captured_at=period_end,
                )
            )
        except Exception as e:
            logger.warning(
                "Skipping competitor during report due to refresh error (user_id=%s competitor_id=%s channel=%s): %s",
                user.id,
                comp.id,
                comp.external_id,
                e,
            )
            continue

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
    logger.info(
        "report_sent user_id=%s tg_user_id=%s report_id=%s sent_at=%s",
        user.id,
        user.tg_user_id,
        report.id,
        _dt_iso(report.sent_at),
    )
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
        unlocked = Schedule.objects.filter(is_running=True, running_started_at__lt=cutoff).update(
            is_running=False,
            running_started_at=None,
            updated_at=now,
        )
        if unlocked:
            logger.warning(
                "schedule_stale_unlock count=%s cutoff=%s",
                unlocked,
                _dt_iso(cutoff),
            )

    due = (
        Schedule.objects.select_related("user")
        .filter(is_enabled=True, is_running=False)
        .filter(next_run_at__lte=now)
    )
    enqueued = 0
    due_count = due.count()
    if due_count:
        logger.info("schedule_tick_due now=%s due_count=%s", _dt_iso(now), due_count)
    for sched in due:
        # Move next_run_at forward to prevent enqueuing again on the next tick.
        try:
            due_at = sched.next_run_at
            sched.next_run_at = compute_next_run_at(sched.user.timezone_str, list(sched.times or []), now)
            sched.save(update_fields=["next_run_at", "updated_at"])
        except Exception:
            logger.exception("Failed to advance schedule next_run_at (schedule_id=%s)", sched.id)
            continue
        due_at_iso = due_at.isoformat() if due_at is not None else ""
        run_user_report.delay(sched.user_id, due_at_iso)
        logger.info(
            "schedule_enqueued user_id=%s schedule_id=%s due_at=%s due_at_local=%s next_run_at=%s next_run_local=%s timezone=%s times=%s",
            sched.user_id,
            sched.id,
            due_at_iso,
            _dt_local(due_at, sched.user.timezone_str),
            _dt_iso(sched.next_run_at),
            _dt_local(sched.next_run_at, sched.user.timezone_str),
            sched.user.timezone_str,
            list(sched.times or []),
        )
        enqueued += 1
    return enqueued


@shared_task(bind=True)
def run_user_report(self, user_id: int, due_at_iso: str = "") -> None:
    now = timezone.now()
    user = TgUser.objects.get(id=user_id)
    due_at = _parse_due_at(due_at_iso)
    if due_at_iso and due_at is None:
        logger.warning("Invalid due_at for run_user_report (user_id=%s due_at_iso=%s)", user_id, due_at_iso)
    retry_no = int(getattr(self.request, "retries", 0))
    max_retries = int(getattr(settings, "SCHEDULE_TASK_MAX_RETRIES", 2))
    retry_delay_sec = int(getattr(settings, "SCHEDULE_TASK_RETRY_DELAY_SECONDS", 90))
    due_tolerance_min = int(getattr(settings, "SCHEDULE_DUE_TOLERANCE_MINUTES", 10))
    retry_window_min = int(getattr(settings, "SCHEDULE_RETRY_WINDOW_MINUTES", due_tolerance_min))

    logger.info(
        "scheduled_report_start user_id=%s tg_user_id=%s due_at=%s retry_no=%s",
        user.id,
        user.tg_user_id,
        _dt_iso(due_at),
        retry_no,
    )

    # Use a transaction for schedule updates to keep period boundaries consistent.
    with transaction.atomic():
        schedule = Schedule.objects.select_for_update().get(user=user)
        if schedule.is_running:
            logger.info("Skipping scheduled report: already running (user_id=%s)", user_id)
            return
        if due_at is not None:
            tolerance = timedelta(minutes=max(1, due_tolerance_min))
            if schedule.last_run_at is not None and due_at <= schedule.last_run_at:
                logger.warning(
                    "Skipping stale scheduled task (user_id=%s due_at=%s last_run_at=%s)",
                    user_id,
                    due_at,
                    schedule.last_run_at,
                )
                return
            if now - due_at > tolerance:
                logger.warning(
                    "Skipping late scheduled task (user_id=%s due_at=%s now=%s lag=%s)",
                    user_id,
                    due_at,
                    now,
                    now - due_at,
                )
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
            attempts=retry_no + 1,
            payload={
                "trigger": "scheduled",
                "due_at": _dt_iso(due_at),
                "retry_no": retry_no,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "schedule_times": list(schedule.times or []),
                "schedule_next_run_at": _dt_iso(schedule.next_run_at),
            },
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
        payload = dict(job.payload or {})
        payload.update({"report_id": report.id if report is not None else None, "retry_no": retry_no})
        job.payload = payload
        job.save(update_fields=["status", "finished_at", "payload"])
        logger.info(
            "scheduled_report_success user_id=%s tg_user_id=%s report_id=%s due_at=%s retry_no=%s",
            user.id,
            user.tg_user_id,
            report.id if report is not None else None,
            _dt_iso(due_at),
            retry_no,
        )
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
                now_fail = timezone.now()
                retry_window = timedelta(minutes=max(1, retry_window_min))
                can_retry = (
                    due_at is not None
                    and retry_no < max_retries
                    and (now_fail - due_at) <= retry_window
                )
                if can_retry:
                    # Keep next_run_at intact (already moved by scheduler); retry same due slot shortly.
                    schedule.save(update_fields=["is_running", "running_started_at", "updated_at"])
                else:
                    # Keep schedule aligned to user slots even after final failure.
                    schedule.next_run_at = compute_next_run_at(
                        user.timezone_str,
                        list(schedule.times or []),
                        now_fail,
                    )
                    schedule.save(update_fields=["is_running", "running_started_at", "next_run_at", "updated_at"])
        except Exception:
            logger.exception("Failed to reschedule after error (user_id=%s)", user_id)

        job.status = JobStatus.FAILED
        job.error = str(e)
        job.finished_at = timezone.now()
        payload = dict(job.payload or {})
        payload.update(
            {
                "retry_planned": (
                    due_at is not None
                    and retry_no < max_retries
                    and (timezone.now() - due_at) <= timedelta(minutes=max(1, retry_window_min))
                ),
                "retry_no": retry_no,
                "max_retries": max_retries,
                "retry_delay_sec": retry_delay_sec,
            }
        )
        job.payload = payload
        job.save(update_fields=["status", "error", "finished_at", "payload"])

        can_retry = (
            due_at is not None
            and retry_no < max_retries
            and (timezone.now() - due_at) <= timedelta(minutes=max(1, retry_window_min))
        )
        if can_retry:
            logger.warning(
                "scheduled_report_retry user_id=%s tg_user_id=%s due_at=%s retry_no=%s/%s delay_sec=%s",
                user.id,
                user.tg_user_id,
                _dt_iso(due_at),
                retry_no + 1,
                max_retries,
                retry_delay_sec,
            )
            raise self.retry(exc=e, countdown=max(1, retry_delay_sec), max_retries=max_retries)

        logger.error(
            "scheduled_report_final_failure user_id=%s tg_user_id=%s due_at=%s retries=%s",
            user.id,
            user.tg_user_id,
            _dt_iso(due_at),
            retry_no,
        )
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
    retry_no = int(getattr(self.request, "retries", 0))
    logger.info(
        "manual_report_start user_id=%s tg_user_id=%s retry_no=%s",
        user.id,
        user.tg_user_id,
        retry_no,
    )

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
            attempts=retry_no + 1,
            payload={
                "trigger": "manual",
                "retry_no": retry_no,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "schedule_times": list(schedule.times or []),
                "schedule_next_run_at": _dt_iso(schedule.next_run_at),
            },
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
        payload = dict(job.payload or {})
        payload.update({"retry_no": retry_no})
        job.payload = payload
        job.save(update_fields=["status", "finished_at", "payload"])
        logger.info(
            "manual_report_success user_id=%s tg_user_id=%s retry_no=%s",
            user.id,
            user.tg_user_id,
            retry_no,
        )
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
                # Manual failures should not shift automatic schedule to arbitrary timestamps.
                schedule.next_run_at = compute_next_run_at(
                    user.timezone_str,
                    list(schedule.times or []),
                    timezone.now(),
                )
                schedule.save(update_fields=["is_running", "running_started_at", "next_run_at", "updated_at"])
        except Exception:
            logger.exception("Failed to reschedule after error (user_id=%s)", user_id)

        job.status = JobStatus.FAILED
        job.error = str(e)
        job.finished_at = timezone.now()
        payload = dict(job.payload or {})
        payload.update({"retry_no": retry_no})
        job.payload = payload
        job.save(update_fields=["status", "error", "finished_at", "payload"])
        logger.error(
            "manual_report_failure user_id=%s tg_user_id=%s retry_no=%s",
            user.id,
            user.tg_user_id,
            retry_no,
        )

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
        max_competitors = int(getattr(settings, "MAX_COMPETITORS_YOUTUBE", 100))
        links = (
            UserCompetitor.objects.select_related("competitor")
            .filter(user=user, is_active=True, competitor__platform=Platform.YOUTUBE)
            .order_by("id")
            .all()[:max_competitors]
        )
        competitors = [lnk.competitor for lnk in links]
        for comp in competitors:
            try:
                refresh_youtube_competitor(competitor=comp, mode="incremental", captured_at=now)
            except Exception as e:
                logger.warning(
                    "Skipping competitor during bootstrap due to refresh error (user_id=%s competitor_id=%s channel=%s): %s",
                    user.id,
                    comp.id,
                    comp.external_id,
                    e,
                )
                continue

        # Mark the baseline point for the first delta window.
        with transaction.atomic():
            schedule = Schedule.objects.select_for_update().get(user=user)
            anchor_last_run_at = _compute_bootstrap_last_run_at(schedule=schedule, captured_at=now)
            if schedule.last_run_at is None or anchor_last_run_at > schedule.last_run_at:
                schedule.last_run_at = anchor_last_run_at
                schedule.save(update_fields=["last_run_at", "updated_at"])
            logger.info(
                "bootstrap_baseline_ready user_id=%s tg_user_id=%s captured_at=%s captured_at_local=%s "
                "anchored_last_run_at=%s anchored_last_run_local=%s next_run_at=%s next_run_local=%s timezone=%s",
                user.id,
                user.tg_user_id,
                _dt_iso(now),
                _dt_local(now, user.timezone_str),
                _dt_iso(schedule.last_run_at),
                _dt_local(schedule.last_run_at, user.timezone_str),
                _dt_iso(schedule.next_run_at),
                _dt_local(schedule.next_run_at, user.timezone_str),
                user.timezone_str,
            )

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
