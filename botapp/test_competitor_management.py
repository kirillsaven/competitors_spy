from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync, sync_to_async

from botapp.handlers import common, competitors
from botapp.state import CompetitorManagementStates
from tracking.adapters.base import SeedResolution
from tracking.models import Competitor, Platform, Report, Schedule, TgUser, UserCompetitor
from tracking.services import report_pipeline, suggested_competitors


class DummyState:
    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def set_state(self, value) -> None:
        self.state = value

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data = {}


class DummyMessage:
    def __init__(self, *, user_id: int, text: str = "") -> None:
        self.text = text
        self.answers: list[str] = []
        self.reply_markups = []
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
        self.reply_markups.append(reply_markup)
        return self

    async def edit_text(self, text: str, reply_markup=None):
        self.answers.append(text)
        self.reply_markups.append(reply_markup)
        return self

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markups.append(reply_markup)
        return self


class DummyCallbackQuery:
    def __init__(self, *, data: str, message: DummyMessage) -> None:
        self.data = data
        self.message = message
        self.from_user = message.from_user
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append((text, show_alert))


async def _db_call(func, *args, **kwargs):
    return await sync_to_async(func, thread_sensitive=True)(*args, **kwargs)


async def _db_run(func, *args, **kwargs):
    return await sync_to_async(func, thread_sensitive=True)(*args, **kwargs)


@pytest.mark.django_db
def test_start_and_help_commands_describe_manual_add_and_suggestions(monkeypatch):
    monkeypatch.setattr(common, "db_call", _db_call)

    start_message = DummyMessage(user_id=9)
    async_to_sync(common.cmd_start)(start_message)
    assert "/competitors_add - добавить конкурентов вручную" in start_message.answers[-1]
    assert "/competitors_suggest - предложить кандидатов" in start_message.answers[-1]

    help_message = DummyMessage(user_id=9)
    async_to_sync(common.cmd_help)(help_message)
    assert "/competitors_add - добавить конкурентов вручную" in help_message.answers[-1]
    assert "/competitors_suggest - показать кандидатов для добавления" in help_message.answers[-1]


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
    assert "/competitors_add - добавить вручную по ссылке или хэндлу" in message.answers[-1]
    assert "/competitors_suggest - выбрать из списка и добавить" in message.answers[-1]
    assert "/competitors_remove - выбрать из списка и убрать" in message.answers[-1]


@pytest.mark.django_db
def test_competitors_suggest_reactivates_from_picker(monkeypatch):
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
        "_load_add_candidates_for_user",
        lambda **kwargs: (
            [
                {
                    "platform": Platform.YOUTUBE,
                    "external_id": "yt-creator",
                    "handle": "creator",
                    "url": "https://www.youtube.com/@creator",
                    "display_name": "Creator",
                    "added_by": "manual",
                    "meta": {},
                }
            ],
            [],
        ),
    )

    state = DummyState()
    message = DummyMessage(user_id=21)
    async_to_sync(competitors.cmd_competitors_suggest)(message, state)

    assert state.state == CompetitorManagementStates.PICK_COMPETITORS_ADD

    toggle = DummyCallbackQuery(data="compadd_toggle:0", message=message)
    async_to_sync(competitors.on_add_toggle)(toggle, state)
    done = DummyCallbackQuery(data="compadd_done", message=message)
    async_to_sync(competitors.on_add_done)(done, state)

    link = async_to_sync(sync_to_async(UserCompetitor.objects.get, thread_sensitive=True))(user=user, competitor=competitor_obj)
    link_count = async_to_sync(sync_to_async(UserCompetitor.objects.filter(user=user, competitor=competitor_obj).count, thread_sensitive=True))()

    assert state.state is None
    assert link.is_active is True
    assert link_count == 1
    assert "Добавлено: 1" in message.answers[-1]
    assert "Пропущено: 0" in message.answers[-1]
    assert "Ошибки: 0" in message.answers[-1]
    assert "YouTube: 1" in message.answers[-1]


@pytest.mark.django_db
def test_competitors_add_enters_manual_wait_state(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=22, tg_chat_id=22)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)

    state = DummyState()
    message = DummyMessage(user_id=22)
    async_to_sync(competitors.cmd_competitors_add)(message, state)

    assert state.state == CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT
    assert "Отправь ссылки или хэндлы конкурентов" in message.answers[-1]


@pytest.mark.django_db
def test_competitor_add_manual_alias_enters_wait_state(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=25, tg_chat_id=25)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)

    state = DummyState()
    message = DummyMessage(user_id=25)
    async_to_sync(competitors.cmd_competitor_add_manual)(message, state)

    assert state.state == CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT
    assert "Отправь ссылки или хэндлы конкурентов" in message.answers[-1]


@pytest.mark.django_db
def test_competitor_add_manual_adds_valid_resolved_seed(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=23, tg_chat_id=23)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)
    def fake_resolve_exact_seed(raw_input, *, context=None):
        assert context is not None
        return SeedResolution(
            platform=Platform.YOUTUBE,
            external_id="yt-manual",
            handle="manual_creator",
            url="https://www.youtube.com/@manual_creator",
            title="Manual Creator",
            description="desc",
            uploads_playlist_id="UUmanual",
        )

    monkeypatch.setattr(competitors, "resolve_exact_seed", fake_resolve_exact_seed)
    monkeypatch.setattr(competitors, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (True, 3))

    state = DummyState()
    async_to_sync(state.update_data)(user_id=user.id)
    async_to_sync(state.set_state)(CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT)
    message = DummyMessage(user_id=23, text="https://www.youtube.com/@manual_creator")

    async_to_sync(competitors.on_competitor_add_manual_input)(message, state)

    link = async_to_sync(sync_to_async(UserCompetitor.objects.select_related("competitor").get, thread_sensitive=True))(user=user)
    assert state.state is None
    assert link.added_by == "manual"
    assert link.competitor.external_id == "yt-manual"
    assert link.competitor.meta["uploads_playlist_id"] == "UUmanual"
    assert "Добавлено вручную: 1" in message.answers[-1]
    assert "Ошибки: 0" in message.answers[-1]
    assert "YouTube: 1" in message.answers[-1]


@pytest.mark.django_db
def test_competitor_add_manual_allows_competitor_over_twenty(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=223, tg_chat_id=223)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])
    for idx in range(20):
        competitor_obj = async_to_sync(sync_to_async(Competitor.objects.create, thread_sensitive=True))(
            platform=Platform.YOUTUBE,
            external_id=f"yt-existing-{idx}",
            handle=f"existing_{idx}",
            display_name=f"Existing {idx}",
            url=f"https://www.youtube.com/@existing_{idx}",
        )
        async_to_sync(sync_to_async(UserCompetitor.objects.create, thread_sensitive=True))(user=user, competitor=competitor_obj, is_active=True)

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)

    def fake_resolve_exact_seed(raw_input, *, context=None):
        assert context is not None
        return SeedResolution(
            platform=Platform.YOUTUBE,
            external_id="yt-manual-21",
            handle="manual_21",
            url="https://www.youtube.com/@manual_21",
            title="Manual 21",
            description="desc",
            uploads_playlist_id="UUmanual21",
        )

    monkeypatch.setattr(competitors, "resolve_exact_seed", fake_resolve_exact_seed)
    monkeypatch.setattr(competitors, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (True, 3))

    state = DummyState()
    async_to_sync(state.update_data)(user_id=user.id)
    async_to_sync(state.set_state)(CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT)
    message = DummyMessage(user_id=223, text="https://www.youtube.com/@manual_21")

    async_to_sync(competitors.on_competitor_add_manual_input)(message, state)

    count = async_to_sync(sync_to_async(UserCompetitor.objects.filter(user=user, is_active=True).count, thread_sensitive=True))()
    assert state.state is None
    assert count == 21
    assert "Добавлено вручную: 1" in message.answers[-1]
    assert "YouTube: 21" in message.answers[-1]


@pytest.mark.django_db
def test_competitor_add_manual_rejects_youtube_without_recent_shorts(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=24, tg_chat_id=24)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)
    def fake_resolve_exact_seed(raw_input, *, context=None):
        assert context is not None
        return SeedResolution(
            platform=Platform.YOUTUBE,
            external_id="yt-reject",
            handle="reject_creator",
            url="https://www.youtube.com/@reject_creator",
            title="Reject Creator",
            description="desc",
            uploads_playlist_id="UUreject",
        )

    monkeypatch.setattr(competitors, "resolve_exact_seed", fake_resolve_exact_seed)
    monkeypatch.setattr(competitors, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (False, 1))

    state = DummyState()
    async_to_sync(state.update_data)(user_id=user.id)
    async_to_sync(state.set_state)(CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT)
    message = DummyMessage(user_id=24, text="@reject_creator")

    async_to_sync(competitors.on_competitor_add_manual_input)(message, state)

    count = async_to_sync(sync_to_async(UserCompetitor.objects.filter(user=user).count, thread_sensitive=True))()
    assert state.state is None
    assert count == 0
    assert "Добавлено вручную: 0" in message.answers[-1]
    assert "Ошибки: 1" in message.answers[-1]
    assert "shorts за 60 дней: 1" in message.answers[-1]


@pytest.mark.django_db
def test_competitors_suggest_allows_add_over_twenty(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=224, tg_chat_id=224)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])
    for idx in range(20):
        competitor_obj = async_to_sync(sync_to_async(Competitor.objects.create, thread_sensitive=True))(
            platform=Platform.YOUTUBE,
            external_id=f"yt-existing-suggest-{idx}",
            handle=f"existing_suggest_{idx}",
            display_name=f"Existing Suggest {idx}",
            url=f"https://www.youtube.com/@existing_suggest_{idx}",
        )
        async_to_sync(sync_to_async(UserCompetitor.objects.create, thread_sensitive=True))(user=user, competitor=competitor_obj, is_active=True)

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)
    monkeypatch.setattr(
        competitors,
        "_load_add_candidates_for_user",
        lambda **kwargs: (
            [
                {
                    "platform": Platform.YOUTUBE,
                    "external_id": "yt-suggest-21",
                    "handle": "suggest_21",
                    "url": "https://www.youtube.com/@suggest_21",
                    "display_name": "Suggest 21",
                    "added_by": "auto",
                    "meta": {},
                }
            ],
            [],
        ),
    )

    state = DummyState()
    message = DummyMessage(user_id=224)
    async_to_sync(competitors.cmd_competitors_suggest)(message, state)
    toggle = DummyCallbackQuery(data="compadd_toggle:0", message=message)
    async_to_sync(competitors.on_add_toggle)(toggle, state)
    done = DummyCallbackQuery(data="compadd_done", message=message)
    async_to_sync(competitors.on_add_done)(done, state)

    count = async_to_sync(sync_to_async(UserCompetitor.objects.filter(user=user, is_active=True).count, thread_sensitive=True))()
    assert state.state is None
    assert count == 21
    assert "Добавлено: 1" in message.answers[-1]
    assert "YouTube: 21" in message.answers[-1]


@pytest.mark.django_db
def test_competitor_add_manual_adds_instagram_share_url_with_keyword_context(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=26, tg_chat_id=26)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)

    def fake_resolve_exact_seed(raw_input, *, context=None):
        assert raw_input == "https://www.instagram.com/eng.lisaa?igsh=Ym5rMHRodGhvZ3oy"
        assert context is not None
        return SeedResolution(
            platform=Platform.INSTAGRAM,
            external_id="ig-manual",
            handle="eng.lisaa",
            url="https://www.instagram.com/eng.lisaa/",
            title="Eng Lisaa",
            description="desc",
            uploads_playlist_id=None,
        )

    monkeypatch.setattr(competitors, "resolve_exact_seed", fake_resolve_exact_seed)

    state = DummyState()
    async_to_sync(state.update_data)(user_id=user.id)
    async_to_sync(state.set_state)(CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT)
    message = DummyMessage(user_id=26, text="https://www.instagram.com/eng.lisaa?igsh=Ym5rMHRodGhvZ3oy")

    async_to_sync(competitors.on_competitor_add_manual_input)(message, state)

    link = async_to_sync(sync_to_async(UserCompetitor.objects.select_related("competitor").get, thread_sensitive=True))(user=user)
    assert state.state is None
    assert link.added_by == "manual"
    assert link.competitor.platform == Platform.INSTAGRAM
    assert link.competitor.external_id == "ig-manual"
    assert "Добавлено вручную: 1" in message.answers[-1]
    assert "Ошибки: 0" in message.answers[-1]
    assert "Instagram: 1" in message.answers[-1]


@pytest.mark.django_db
def test_competitors_command_shows_more_than_twenty_active_competitors(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=227, tg_chat_id=227)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])
    for idx in range(21):
        competitor_obj = async_to_sync(sync_to_async(Competitor.objects.create, thread_sensitive=True))(
            platform=Platform.YOUTUBE,
            external_id=f"yt-list-{idx}",
            handle=f"ytlist{idx}",
            display_name=f"YT List {idx}",
            url=f"https://www.youtube.com/@ytlist{idx}",
        )
        async_to_sync(sync_to_async(UserCompetitor.objects.create, thread_sensitive=True))(user=user, competitor=competitor_obj, is_active=True)

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)

    message = DummyMessage(user_id=227)
    async_to_sync(competitors.cmd_competitors)(message)

    assert "YouTube (21):" in message.answers[-1]
    assert "21. YT List 20 (@ytlist20)" in message.answers[-1]


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

    state = DummyState()
    message = DummyMessage(user_id=31)
    async_to_sync(competitors.cmd_competitors_remove)(message, state)

    assert state.state == CompetitorManagementStates.PICK_COMPETITORS_REMOVE

    toggle = DummyCallbackQuery(data=f"comprem_toggle:{youtube.id}", message=message)
    async_to_sync(competitors.on_remove_toggle)(toggle, state)
    done = DummyCallbackQuery(data="comprem_done", message=message)
    async_to_sync(competitors.on_remove_done)(done, state)

    user_link = async_to_sync(sync_to_async(UserCompetitor.objects.get, thread_sensitive=True))(user=user, competitor=youtube)
    other_link = async_to_sync(sync_to_async(UserCompetitor.objects.get, thread_sensitive=True))(user=other_user, competitor=youtube)
    remaining = async_to_sync(sync_to_async(report_pipeline.get_active_competitors, thread_sensitive=True))(user=user)

    assert state.state is None
    assert user_link.is_active is False
    assert other_link.is_active is True
    assert Competitor.objects.filter(id=youtube.id).exists()
    assert [(item.platform, item.external_id) for item in remaining] == [(Platform.INSTAGRAM, "ig-keep")]
    assert "Удалено: 1" in message.answers[-1]
    assert "Пропущено: 0" in message.answers[-1]
    assert "YouTube: 0" in message.answers[-1]
    assert "Instagram: 1" in message.answers[-1]


@pytest.mark.django_db
def test_suggested_youtube_add_reactivates_competitor(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=331, tg_chat_id=331)
    competitor_obj = async_to_sync(sync_to_async(Competitor.objects.create, thread_sensitive=True))(
        platform=Platform.YOUTUBE,
        external_id="yt-suggested-reactivate",
        handle="suggested_reactivate",
        display_name="Suggested Reactivate",
        url="https://www.youtube.com/@suggested_reactivate",
        meta={"uploads_playlist_id": "UUreactivate"},
    )
    async_to_sync(sync_to_async(UserCompetitor.objects.create, thread_sensitive=True))(
        user=user,
        competitor=competitor_obj,
        is_active=False,
        added_by="auto",
    )
    report = async_to_sync(sync_to_async(Report.objects.create, thread_sensitive=True))(
        user=user,
        period_start=datetime(2026, 3, 30, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
        status="sent",
        payload={
            "sections": [],
            "suggested_competitors": {
                "youtube": {
                    "items": [
                        {
                            "channel_id": "yt-suggested-reactivate",
                            "channel_title": "Suggested Reactivate",
                            "channel_url": "https://www.youtube.com/channel/yt-suggested-reactivate",
                            "appearance_count": 2,
                        }
                    ]
                }
            },
        },
    )

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)

    def fake_resolve_exact_seed(raw_input, *, context=None):
        assert context is not None
        assert raw_input == "https://www.youtube.com/channel/yt-suggested-reactivate"
        return SeedResolution(
            platform=Platform.YOUTUBE,
            external_id="yt-suggested-reactivate",
            handle="suggested_reactivate",
            url="https://www.youtube.com/@suggested_reactivate",
            title="Suggested Reactivate",
            description="desc",
            uploads_playlist_id="UUreactivate",
        )

    monkeypatch.setattr(suggested_competitors, "resolve_exact_seed", fake_resolve_exact_seed)
    monkeypatch.setattr(suggested_competitors, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (True, 3))

    message = DummyMessage(user_id=331)
    callback = DummyCallbackQuery(data=f"suggytadd:{report.id}:0", message=message)
    async_to_sync(competitors.on_suggested_youtube_add)(callback)

    link = async_to_sync(sync_to_async(UserCompetitor.objects.get, thread_sensitive=True))(user=user, competitor=competitor_obj)
    assert link.is_active is True
    assert "Добавил конкурента в YouTube: Suggested Reactivate." in message.answers[-1]


@pytest.mark.django_db
def test_suggested_youtube_add_is_idempotent_and_can_grow_active_list_beyond_twenty(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=332, tg_chat_id=332)
    for idx in range(20):
        competitor_obj = async_to_sync(sync_to_async(Competitor.objects.create, thread_sensitive=True))(
            platform=Platform.YOUTUBE,
            external_id=f"yt-existing-suggested-{idx}",
            handle=f"existing_suggested_{idx}",
            display_name=f"Existing Suggested {idx}",
            url=f"https://www.youtube.com/@existing_suggested_{idx}",
            meta={"uploads_playlist_id": f"UUexisting{idx}"},
        )
        async_to_sync(sync_to_async(UserCompetitor.objects.create, thread_sensitive=True))(user=user, competitor=competitor_obj, is_active=True)

    report = async_to_sync(sync_to_async(Report.objects.create, thread_sensitive=True))(
        user=user,
        period_start=datetime(2026, 3, 30, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
        status="sent",
        payload={
            "sections": [],
            "suggested_competitors": {
                "youtube": {
                    "items": [
                        {
                            "channel_id": "yt-suggested-21",
                            "channel_title": "Suggested 21",
                            "channel_url": "https://www.youtube.com/channel/yt-suggested-21",
                            "appearance_count": 3,
                        }
                    ]
                }
            },
        },
    )

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)

    def fake_resolve_exact_seed(raw_input, *, context=None):
        assert context is not None
        assert raw_input == "https://www.youtube.com/channel/yt-suggested-21"
        return SeedResolution(
            platform=Platform.YOUTUBE,
            external_id="yt-suggested-21",
            handle="suggested_21",
            url="https://www.youtube.com/@suggested_21",
            title="Suggested 21",
            description="desc",
            uploads_playlist_id="UUsuggested21",
        )

    monkeypatch.setattr(suggested_competitors, "resolve_exact_seed", fake_resolve_exact_seed)
    monkeypatch.setattr(suggested_competitors, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (True, 3))

    message = DummyMessage(user_id=332)
    callback = DummyCallbackQuery(data=f"suggytadd:{report.id}:0", message=message)
    async_to_sync(competitors.on_suggested_youtube_add)(callback)
    async_to_sync(competitors.on_suggested_youtube_add)(callback)

    count = async_to_sync(sync_to_async(UserCompetitor.objects.filter(user=user, is_active=True).count, thread_sensitive=True))()
    assert count == 21
    assert any("Активных YouTube-конкурентов: 21" in answer for answer in message.answers)
    assert message.answers[-1] == "Suggested 21 уже есть в активных YouTube-конкурентах."
