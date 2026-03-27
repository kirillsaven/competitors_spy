from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tracking import tasks as task_module
from tracking.models import Report, ReportStatus, Schedule, TgUser


@pytest.mark.django_db
def test_setup_bootstrap_does_not_suppress_first_real_scheduled_report(monkeypatch: pytest.MonkeyPatch) -> None:
    user = TgUser.objects.create(tg_user_id=101, tg_chat_id=101, timezone_str="Asia/Bangkok")
    due_at = datetime(2026, 3, 26, 12, 45, tzinfo=UTC)  # 19:45 local
    Schedule.objects.create(user=user, is_enabled=True, times=["19:45"], next_run_at=due_at)

    bootstrap_now = due_at + timedelta(minutes=2)
    monkeypatch.setattr(task_module.timezone, "now", lambda: bootstrap_now)
    monkeypatch.setattr(task_module, "refresh_competitor", lambda competitor, mode, captured_at: [])

    task_module.bootstrap_user_data.run(user.id)

    schedule = Schedule.objects.get(user=user)
    assert schedule.next_run_at == due_at
    assert schedule.last_run_at == due_at - timedelta(seconds=1)

    scheduled_run_now = due_at + timedelta(minutes=3)
    monkeypatch.setattr(task_module.timezone, "now", lambda: scheduled_run_now)

    seen: dict[str, datetime] = {}

    def fake_generate_and_send_report(*, user: TgUser, period_start: datetime, period_end: datetime) -> Report:
        seen["period_start"] = period_start
        seen["period_end"] = period_end
        return Report.objects.create(
            user=user,
            period_start=period_start,
            period_end=period_end,
            status=ReportStatus.CREATED,
            payload={},
        )

    monkeypatch.setattr(task_module, "_generate_and_send_report", fake_generate_and_send_report)

    task_module.run_user_report.run(user.id, due_at.isoformat())

    schedule.refresh_from_db()
    assert seen["period_start"] == due_at - timedelta(seconds=1)
    assert seen["period_end"] == scheduled_run_now
    assert schedule.last_run_at == scheduled_run_now
    assert schedule.next_run_at == due_at + timedelta(days=1)


@pytest.mark.django_db
def test_tick_due_schedules_enqueues_due_job(monkeypatch: pytest.MonkeyPatch) -> None:
    user = TgUser.objects.create(tg_user_id=202, tg_chat_id=202, timezone_str="Asia/Bangkok")
    due_at = datetime(2026, 3, 26, 12, 45, tzinfo=UTC)  # 19:45 local
    Schedule.objects.create(user=user, is_enabled=True, times=["19:45"], next_run_at=due_at)

    monkeypatch.setattr(task_module.timezone, "now", lambda: due_at)
    enqueued_calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        task_module.run_user_report,
        "delay",
        lambda user_id, due_at_iso: enqueued_calls.append((user_id, due_at_iso)),
    )

    enqueued = task_module.tick_due_schedules.run()

    schedule = Schedule.objects.get(user=user)
    assert enqueued == 1
    assert enqueued_calls == [(user.id, due_at.isoformat())]
    assert schedule.next_run_at == due_at + timedelta(days=1)
