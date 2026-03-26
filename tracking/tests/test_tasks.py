from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from tracking.models import JobRun, JobStatus, Platform, Schedule, TgUser
from tracking import tasks


def _make_user_with_schedule(*, tg_user_id: int = 1) -> tuple[TgUser, Schedule]:
    user = TgUser.objects.create(
        tg_user_id=tg_user_id,
        tg_chat_id=tg_user_id,
        timezone_str="Europe/Moscow",
    )
    schedule = Schedule.objects.create(
        user=user,
        is_enabled=True,
        times=["09:00"],
        next_run_at=timezone.now() + timedelta(hours=1),
    )
    return user, schedule


@pytest.mark.django_db
def test_run_user_report_now_sends_message_when_already_running(monkeypatch):
    user, schedule = _make_user_with_schedule(tg_user_id=9001)
    schedule.is_running = True
    schedule.running_started_at = timezone.now()
    schedule.save(update_fields=["is_running", "running_started_at", "updated_at"])

    sent: list[str] = []
    monkeypatch.setattr(tasks, "send_message", lambda *, chat_id, text: sent.append(text) or {"message_id": 1})

    tasks.run_user_report_now.run(user.id, trigger="setup")

    assert sent == [
        "Первый отчет после настройки уже собирается. Пришлю его отдельным сообщением, когда он будет готов."
    ]
    assert JobRun.objects.filter(user=user, job_type="run_user_report_now").count() == 0


@pytest.mark.django_db
def test_run_user_report_now_sends_explicit_failure_message(monkeypatch):
    user, schedule = _make_user_with_schedule(tg_user_id=9002)
    sent: list[str] = []
    delayed: list[tuple[tuple, dict]] = []

    monkeypatch.setattr(
        tasks,
        "_generate_and_send_report",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("forced smoke failure")),
    )
    monkeypatch.setattr(tasks, "send_message", lambda *, chat_id, text: sent.append(text) or {"message_id": 2})
    monkeypatch.setattr(
        tasks.notify_report_still_running,
        "apply_async",
        lambda *args, **kwargs: delayed.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="forced smoke failure"):
        tasks.run_user_report_now.run(user.id, trigger="setup")

    schedule.refresh_from_db()
    job = JobRun.objects.get(user=user, job_type="run_user_report_now")
    assert job.status == JobStatus.FAILED
    assert schedule.is_running is False
    assert delayed == [
        (
            (),
            {
                "args": [user.id, job.id],
                "kwargs": {"trigger": "setup"},
                "countdown": tasks.FIRST_REPORT_STATUS_DELAY_SECONDS,
            },
        )
    ]
    assert sent[-1] == "Не получилось собрать первый отчет после настройки.\nПричина: forced smoke failure"


@pytest.mark.django_db
def test_notify_report_still_running_sends_setup_status(monkeypatch):
    user, schedule = _make_user_with_schedule(tg_user_id=9003)
    schedule.is_running = True
    schedule.running_started_at = timezone.now()
    schedule.save(update_fields=["is_running", "running_started_at", "updated_at"])
    job = JobRun.objects.create(
        job_type="run_user_report_now",
        user=user,
        status=JobStatus.RUNNING,
        started_at=timezone.now(),
        attempts=1,
        payload={"trigger": "setup"},
    )
    sent: list[str] = []
    monkeypatch.setattr(tasks, "send_message", lambda *, chat_id, text: sent.append(text) or {"message_id": 3})

    tasks.notify_report_still_running.run(user.id, job.id, trigger="setup")

    assert sent == [
        "Первый отчет после настройки все еще собирается. Это занимает дольше обычного, пришлю его отдельным сообщением или напишу, если сборка не получится."
    ]


@pytest.mark.django_db
def test_setup_finalize_schedule_triggers_setup_report_with_follow_up(monkeypatch):
    from asgiref.sync import async_to_sync
    from asgiref.sync import sync_to_async
    from botapp.handlers import setup as setup_handler

    class DummyState:
        def __init__(self, data: dict):
            self.data = dict(data)
            self.state = None

        async def get_data(self):
            return dict(self.data)

        async def update_data(self, **kwargs):
            self.data.update(kwargs)

        async def set_state(self, value):
            self.state = value

        async def clear(self):
            self.data.clear()
            self.state = None

    class DummyMessage:
        def __init__(self):
            self.answers: list[str] = []
            self.chat = SimpleNamespace(id=1)
            self.bot = SimpleNamespace(delete_message=lambda *args, **kwargs: None)

        async def answer(self, text: str, reply_markup=None):
            self.answers.append(text)
            return SimpleNamespace(chat=self.chat, message_id=len(self.answers), bot=self.bot)

    user, schedule = _make_user_with_schedule(tg_user_id=9004)
    state = DummyState({"user_id": user.id, "times": ["09:00"], "setup_runtime_id": "runtime-1"})
    message = DummyMessage()
    delayed: list[tuple[tuple, dict]] = []

    async def fake_db_call(func, *args, **kwargs):
        return await sync_to_async(func)(*args, **kwargs)

    async def fake_db_run(func, *args, **kwargs):
        return await sync_to_async(func)(*args, **kwargs)

    monkeypatch.setattr(setup_handler, "db_call", fake_db_call)
    monkeypatch.setattr(setup_handler, "db_run", fake_db_run)
    monkeypatch.setattr(setup_handler, "format_dt_local", lambda *args, **kwargs: "soon")
    monkeypatch.setattr(setup_handler, "_build_active_competitor_summary", lambda *, counts: ["YouTube: 0", "TikTok: 0", "Instagram: 0"])
    monkeypatch.setattr(
        setup_handler.run_user_report_now,
        "delay",
        lambda *args, **kwargs: delayed.append((args, kwargs)),
    )

    async_to_sync(setup_handler._finalize_schedule)(message, state)

    assert delayed == [((user.id,), {"trigger": "setup"})]
    assert "Сейчас соберу первый отчет, чтобы все проверить." in message.answers[-1]
