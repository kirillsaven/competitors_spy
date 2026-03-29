from __future__ import annotations

from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync, sync_to_async

from botapp.handlers import competitors
from botapp.state import CompetitorManagementStates
from tracking.adapters.base import SeedResolution
from tracking.models import Competitor, Platform, Schedule, TgUser, UserCompetitor
from tracking.services import report_pipeline


class DummyState:
    def __init__(self) -> None:
        self.state = None

    async def set_state(self, value) -> None:
        self.state = value


class DummyMessage:
    def __init__(self, *, user_id: int, text: str = "") -> None:
        self.text = text
        self.answers: list[str] = []
        self.chat = SimpleNamespace(id=user_id)
        self.from_user = SimpleNamespace(
            id=user_id,
            username=f"user{user_id}",
            first_name="Test",
            last_name="User",
            language_code="ru",
        )

    async def answer(self, text: str, reply_markup=None):
        self.answers.append(text)
        return SimpleNamespace(chat=self.chat, message_id=len(self.answers))


async def _db_call(func, *args, **kwargs):
    return await sync_to_async(func, thread_sensitive=True)(*args, **kwargs)


async def _db_run(func, *args, **kwargs):
    return await sync_to_async(func, thread_sensitive=True)(*args, **kwargs)


@pytest.mark.django_db
def test_competitors_command_groups_active_competitors(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=11, tg_chat_id=11)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])
    youtube = async_to_sync(sync_to_async(Competitor.objects.create, thread_sensitive=True))(
        platform=Platform.YOUTUBE,
        external_id="yt-1",
        handle="mrbeast",
        display_name="MrBeast",
        url="https://www.youtube.com/@mrbeast",
    )
    instagram = async_to_sync(sync_to_async(Competitor.objects.create, thread_sensitive=True))(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="nasa",
        display_name="NASA",
        url="https://www.instagram.com/nasa/",
    )
    async_to_sync(sync_to_async(UserCompetitor.objects.create, thread_sensitive=True))(user=user, competitor=youtube, is_active=True)
    async_to_sync(sync_to_async(UserCompetitor.objects.create, thread_sensitive=True))(user=user, competitor=instagram, is_active=True)

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)

    message = DummyMessage(user_id=11)
    async_to_sync(competitors.cmd_competitors)(message)

    assert "Активные конкуренты:" in message.answers[-1]
    assert "YouTube (1):" in message.answers[-1]
    assert "1. MrBeast (@mrbeast)" in message.answers[-1]
    assert "TikTok (0):" in message.answers[-1]
    assert "Instagram (1):" in message.answers[-1]
    assert "/competitors_add - добавить вручную" in message.answers[-1]
    assert "/competitors_remove - убрать из списка" in message.answers[-1]


@pytest.mark.django_db
def test_competitors_add_reactivates_idempotently(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=21, tg_chat_id=21)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])
    competitor_obj = async_to_sync(sync_to_async(Competitor.objects.create, thread_sensitive=True))(
        platform=Platform.YOUTUBE,
        external_id="yt-creator",
        handle="creator",
        display_name="Creator",
        url="https://www.youtube.com/@creator",
    )
    async_to_sync(sync_to_async(UserCompetitor.objects.create, thread_sensitive=True))(
        user=user,
        competitor=competitor_obj,
        is_active=False,
    )

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)
    monkeypatch.setattr(
        competitors,
        "resolve_exact_seed",
        lambda raw_input: SeedResolution(
            platform=Platform.YOUTUBE,
            external_id="yt-creator",
            handle="creator",
            url="https://www.youtube.com/@creator",
            title="Creator",
            description="Channel",
            uploads_playlist_id="UU123",
        ),
    )

    state = DummyState()
    command_message = DummyMessage(user_id=21)
    async_to_sync(competitors.cmd_competitors_add)(command_message, state)

    assert state.state == CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT

    input_message = DummyMessage(user_id=21, text="@creator\n@creator")
    async_to_sync(competitors.on_competitors_add_input)(input_message, state)

    link = async_to_sync(sync_to_async(UserCompetitor.objects.get, thread_sensitive=True))(user=user, competitor=competitor_obj)
    link_count = async_to_sync(sync_to_async(UserCompetitor.objects.filter(user=user, competitor=competitor_obj).count, thread_sensitive=True))()

    assert state.state is None
    assert link.is_active is True
    assert link_count == 1
    assert "Добавлено: 1" in input_message.answers[-1]
    assert "Пропущено: 1" in input_message.answers[-1]
    assert "Ошибки: 0" in input_message.answers[-1]
    assert "YouTube: 1" in input_message.answers[-1]


@pytest.mark.django_db
def test_competitors_remove_deactivates_only_user_link_and_updates_report_active_list(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=31, tg_chat_id=31)
    other_user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=32, tg_chat_id=32)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=other_user, times=["09:00"])

    youtube = async_to_sync(sync_to_async(Competitor.objects.create, thread_sensitive=True))(
        platform=Platform.YOUTUBE,
        external_id="yt-keep",
        handle="creator",
        display_name="Creator",
        url="https://www.youtube.com/@creator",
    )
    instagram = async_to_sync(sync_to_async(Competitor.objects.create, thread_sensitive=True))(
        platform=Platform.INSTAGRAM,
        external_id="ig-keep",
        handle="nasa",
        display_name="NASA",
        url="https://www.instagram.com/nasa/",
    )
    async_to_sync(sync_to_async(UserCompetitor.objects.create, thread_sensitive=True))(user=user, competitor=youtube, is_active=True)
    async_to_sync(sync_to_async(UserCompetitor.objects.create, thread_sensitive=True))(user=user, competitor=instagram, is_active=True)
    async_to_sync(sync_to_async(UserCompetitor.objects.create, thread_sensitive=True))(user=other_user, competitor=youtube, is_active=True)

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)
    monkeypatch.setattr(
        competitors,
        "resolve_exact_seed",
        lambda raw_input: SeedResolution(
            platform=Platform.YOUTUBE,
            external_id="yt-keep",
            handle="creator",
            url="https://www.youtube.com/@creator",
            title="Creator",
            description="Channel",
            uploads_playlist_id="UU999",
        ),
    )

    state = DummyState()
    command_message = DummyMessage(user_id=31)
    async_to_sync(competitors.cmd_competitors_remove)(command_message, state)

    assert state.state == CompetitorManagementStates.WAIT_COMPETITORS_REMOVE_INPUT

    input_message = DummyMessage(user_id=31, text="https://www.youtube.com/@creator")
    async_to_sync(competitors.on_competitors_remove_input)(input_message, state)

    user_link = async_to_sync(sync_to_async(UserCompetitor.objects.get, thread_sensitive=True))(user=user, competitor=youtube)
    other_link = async_to_sync(sync_to_async(UserCompetitor.objects.get, thread_sensitive=True))(user=other_user, competitor=youtube)
    remaining = async_to_sync(sync_to_async(report_pipeline.get_active_competitors, thread_sensitive=True))(user=user)

    assert state.state is None
    assert user_link.is_active is False
    assert other_link.is_active is True
    assert Competitor.objects.filter(id=youtube.id).exists()
    assert [(item.platform, item.external_id) for item in remaining] == [(Platform.INSTAGRAM, "ig-keep")]
    assert "Удалено: 1" in input_message.answers[-1]
    assert "Пропущено: 0" in input_message.answers[-1]
    assert "YouTube: 0" in input_message.answers[-1]
    assert "Instagram: 1" in input_message.answers[-1]
