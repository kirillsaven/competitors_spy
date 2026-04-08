from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync, sync_to_async

from botapp.handlers import common, competitors
from botapp.state import CompetitorManagementStates
from tracking.adapters.base import SeedResolution
from tracking.models import Competitor, Platform, Report, Schedule, SeedProfile, SeedStatus, TgUser, UserCompetitor
from tracking.services import report_pipeline, suggested_competitors
from tracking.services.setup_retry_cache import clear_retry_cache


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
        self.edit_text_calls = 0
        self.edit_reply_markup_calls = 0

    async def answer(self, text: str, reply_markup=None):
        self.answers.append(text)
        self.reply_markups.append(reply_markup)
        return self

    async def edit_text(self, text: str, reply_markup=None):
        self.edit_text_calls += 1
        self.answers.append(text)
        self.reply_markups.append(reply_markup)
        return self

    async def edit_reply_markup(self, reply_markup=None):
        self.edit_reply_markup_calls += 1
        self.reply_markups.append(reply_markup)
        return self


class DummyCallbackQuery:
    def __init__(self, *, data: str, message: DummyMessage, fail_answer: bool = False) -> None:
        self.data = data
        self.message = message
        self.from_user = message.from_user
        self.fail_answer = fail_answer
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        if self.fail_answer:
            raise RuntimeError("stale callback")
        self.answers.append((text, show_alert))


async def _db_call(func, *args, **kwargs):
    return await sync_to_async(func, thread_sensitive=True)(*args, **kwargs)


async def _db_run(func, *args, **kwargs):
    return await sync_to_async(func, thread_sensitive=True)(*args, **kwargs)


def _candidate(idx: int, *, platform: str = Platform.YOUTUBE) -> dict:
    handle = f"candidate_{idx}"
    return {
        "platform": platform,
        "external_id": f"{platform}-{idx}",
        "handle": handle,
        "url": f"https://example.com/{handle}",
        "display_name": f"Candidate {idx}",
        "added_by": "auto",
        "meta": {},
    }


def _button_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _create_resolved_seed_profile(*, user: TgUser, raw_input: str = "@creator", canonical_url: str = "https://www.youtube.com/@creator", keywords: list[str] | None = None):
    return async_to_sync(sync_to_async(SeedProfile.objects.create, thread_sensitive=True))(
        user=user,
        raw_input=raw_input,
        canonical_url=canonical_url,
        niche_keywords=list(keywords or ["finance"]),
        status=SeedStatus.RESOLVED,
        detected_platform=Platform.YOUTUBE,
    )


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
        "_load_add_candidates_for_user_cached",
        lambda **kwargs: competitors.AddCandidateLoadResult(
            candidates=[
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
            notes=[],
            cache_hit=False,
            discovery_build_ms=0.0,
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
def test_competitors_suggest_second_open_reuses_cache(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=121, tg_chat_id=121)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])
    _create_resolved_seed_profile(user=user, raw_input="@cached", canonical_url="https://www.youtube.com/@cached")

    calls = {"count": 0}

    def fake_load(*, user):
        calls["count"] += 1
        return (
            [
                {
                    "platform": Platform.YOUTUBE,
                    "external_id": "yt-cached",
                    "handle": "cached",
                    "url": "https://www.youtube.com/@cached",
                    "display_name": "Cached",
                    "added_by": "auto",
                    "meta": {},
                }
            ],
            ["note"],
        )

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)
    monkeypatch.setattr(competitors, "_load_add_candidates_for_user", fake_load)
    clear_retry_cache()

    first = competitors._load_add_candidates_for_user_cached(user=user)
    second = competitors._load_add_candidates_for_user_cached(user=user)

    assert calls["count"] == 1
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first.candidates == second.candidates
    assert first.notes == second.notes
    clear_retry_cache()


@pytest.mark.django_db
def test_competitors_suggest_cache_invalidates_when_seed_changes(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=122, tg_chat_id=122)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])
    _create_resolved_seed_profile(user=user, raw_input="@first", canonical_url="https://www.youtube.com/@first", keywords=["alpha"])

    calls = {"count": 0}

    def fake_load(*, user):
        calls["count"] += 1
        return (
            [
                {
                    "platform": Platform.YOUTUBE,
                    "external_id": f"yt-cached-{calls['count']}",
                    "handle": f"cached_{calls['count']}",
                    "url": f"https://www.youtube.com/@cached_{calls['count']}",
                    "display_name": f"Cached {calls['count']}",
                    "added_by": "auto",
                    "meta": {},
                }
            ],
            [],
        )

    monkeypatch.setattr(competitors, "_load_add_candidates_for_user", fake_load)
    clear_retry_cache()

    first = competitors._load_add_candidates_for_user_cached(user=user)
    _create_resolved_seed_profile(
        user=user,
        raw_input="@second",
        canonical_url="https://www.youtube.com/@second",
        keywords=["beta"],
    )
    second = competitors._load_add_candidates_for_user_cached(user=user)

    assert calls["count"] == 2
    assert first.cache_hit is False
    assert second.cache_hit is False
    assert first.candidates != second.candidates
    clear_retry_cache()


def test_competitor_add_picker_page_forward_back_uses_markup_only():
    candidates = [_candidate(idx) for idx in range(10)]
    state = DummyState()
    async_to_sync(state.update_data)(
        user_id=101,
        competitor_add_candidates=candidates,
        competitor_add_picker_rows=competitors._make_add_picker_rows(candidates),
        competitor_add_platform_by_id=competitors._make_add_platform_by_id(candidates),
        competitor_add_selected_ids=[],
        competitor_add_page=0,
        competitor_add_notes=[],
    )
    message = DummyMessage(user_id=101)

    next_page = DummyCallbackQuery(data="compadd_page:1", message=message)
    async_to_sync(competitors.on_add_page)(next_page, state)

    assert state.data["competitor_add_page"] == 1
    assert message.edit_text_calls == 0
    assert message.edit_reply_markup_calls == 1
    assert any("Candidate 8" in text for text in _button_texts(message.reply_markups[-1]))

    prev_page = DummyCallbackQuery(data="compadd_page:0", message=message)
    async_to_sync(competitors.on_add_page)(prev_page, state)

    assert state.data["competitor_add_page"] == 0
    assert message.edit_text_calls == 0
    assert message.edit_reply_markup_calls == 2
    assert any("Candidate 0" in text for text in _button_texts(message.reply_markups[-1]))


def test_competitor_add_picker_failed_ack_does_not_abort_page_render():
    candidates = [_candidate(idx) for idx in range(10)]
    state = DummyState()
    async_to_sync(state.update_data)(
        user_id=103,
        competitor_add_candidates=candidates,
        competitor_add_picker_rows=competitors._make_add_picker_rows(candidates),
        competitor_add_platform_by_id=competitors._make_add_platform_by_id(candidates),
        competitor_add_selected_ids=[],
        competitor_add_page=0,
        competitor_add_notes=[],
    )
    message = DummyMessage(user_id=103)

    callback = DummyCallbackQuery(data="compadd_page:1", message=message, fail_answer=True)
    async_to_sync(competitors.on_add_page)(callback, state)

    assert state.data["competitor_add_page"] == 1
    assert message.edit_reply_markup_calls == 1
    assert any("Candidate 8" in text for text in _button_texts(message.reply_markups[-1]))


def test_competitor_add_picker_toggle_uses_markup_only():
    candidates = [_candidate(idx) for idx in range(10)]
    state = DummyState()
    async_to_sync(state.update_data)(
        user_id=104,
        competitor_add_candidates=candidates,
        competitor_add_picker_rows=competitors._make_add_picker_rows(candidates),
        competitor_add_platform_by_id=competitors._make_add_platform_by_id(candidates),
        competitor_add_selected_ids=[],
        competitor_add_page=0,
        competitor_add_notes=[],
    )
    message = DummyMessage(user_id=104)

    async_to_sync(competitors.on_add_toggle)(DummyCallbackQuery(data="compadd_toggle:0", message=message), state)

    assert state.data["competitor_add_selected_ids"] == [0]
    assert message.edit_text_calls == 0
    assert message.edit_reply_markup_calls == 1
    assert any(text.startswith("✅ 1. [YT] Candidate 0") for text in _button_texts(message.reply_markups[-1]))
    assert "Добавить (1)" in _button_texts(message.reply_markups[-1])


def test_competitor_add_picker_selection_persists_across_pages():
    candidates = [_candidate(idx) for idx in range(10)]
    state = DummyState()
    async_to_sync(state.update_data)(
        user_id=102,
        competitor_add_candidates=candidates,
        competitor_add_picker_rows=competitors._make_add_picker_rows(candidates),
        competitor_add_platform_by_id=competitors._make_add_platform_by_id(candidates),
        competitor_add_selected_ids=[],
        competitor_add_page=0,
        competitor_add_notes=[],
    )
    message = DummyMessage(user_id=102)

    async_to_sync(competitors.on_add_toggle)(DummyCallbackQuery(data="compadd_toggle:0", message=message), state)
    async_to_sync(competitors.on_add_page)(DummyCallbackQuery(data="compadd_page:1", message=message), state)
    async_to_sync(competitors.on_add_toggle)(DummyCallbackQuery(data="compadd_toggle:8", message=message), state)
    async_to_sync(competitors.on_add_page)(DummyCallbackQuery(data="compadd_page:0", message=message), state)

    assert state.data["competitor_add_selected_ids"] == [0, 8]
    assert state.data["competitor_add_page"] == 0
    assert message.edit_text_calls == 0
    assert message.edit_reply_markup_calls == 4
    assert any(text.startswith("✅ 1. [YT] Candidate 0") for text in _button_texts(message.reply_markups[-1]))
    assert "Добавить (2)" in _button_texts(message.reply_markups[-1])


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
        "_load_add_candidates_for_user_cached",
        lambda **kwargs: competitors.AddCandidateLoadResult(
            candidates=[
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
            notes=[],
            cache_hit=False,
            discovery_build_ms=0.0,
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

    def fake_resolve_instagram_seeds_batch(raw_inputs, *, context=None):
        assert raw_inputs == ["https://www.instagram.com/eng.lisaa?igsh=Ym5rMHRodGhvZ3oy"]
        assert context is not None
        return [
            SeedResolution(
                platform=Platform.INSTAGRAM,
                external_id="ig-manual",
                handle="eng.lisaa",
                url="https://www.instagram.com/eng.lisaa/",
                title="Eng Lisaa",
                description="desc",
                uploads_playlist_id=None,
            )
        ]

    monkeypatch.setattr(competitors, "resolve_instagram_seeds_batch", fake_resolve_instagram_seeds_batch)

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
def test_competitor_add_manual_batches_multiple_instagram_inputs(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=228, tg_chat_id=228)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)

    batched_calls: list[list[str]] = []

    def fake_resolve_instagram_seeds_batch(raw_inputs, *, context=None):
        assert context is not None
        batched_calls.append(list(raw_inputs))
        out: list[SeedResolution | None] = []
        for idx, raw_input in enumerate(raw_inputs, start=1):
            handle = f"batched_{idx}"
            out.append(
                SeedResolution(
                    platform=Platform.INSTAGRAM,
                    external_id=f"ig-batch-{idx}",
                    handle=handle,
                    url=f"https://www.instagram.com/{handle}/",
                    title=f"IG Batch {idx}",
                    description=raw_input,
                    uploads_playlist_id=None,
                )
            )
        return out

    monkeypatch.setattr(competitors, "resolve_instagram_seeds_batch", fake_resolve_instagram_seeds_batch)
    monkeypatch.setattr(
        competitors,
        "resolve_exact_seed",
        lambda raw_input, *, context=None: pytest.fail(f"resolve_exact_seed should not run for pure IG batch: {raw_input}"),
    )

    state = DummyState()
    async_to_sync(state.update_data)(user_id=user.id)
    async_to_sync(state.set_state)(CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT)
    message = DummyMessage(
        user_id=228,
        text="\n".join(
            [
                "https://www.instagram.com/batch.one/?igsh=abc111",
                "https://instagram.com/batch.two?igsh=abc222",
                "https://www.instagram.com/batch.three/",
                "https://instagram.com/batch.four?igsh=abc444",
                "https://www.instagram.com/batch.five/?igsh=abc555",
            ]
        ),
    )

    async_to_sync(competitors.on_competitor_add_manual_input)(message, state)

    count = async_to_sync(sync_to_async(UserCompetitor.objects.filter(user=user, is_active=True).count, thread_sensitive=True))()
    assert state.state is None
    assert len(batched_calls) == 1
    assert len(batched_calls[0]) == 5
    assert count == 5
    assert "Проверяю профили..." in message.answers[0]
    assert "Добавлено вручную: 5" in message.answers[-1]
    assert "Ошибки: 0" in message.answers[-1]
    assert "Instagram: 5" in message.answers[-1]


@pytest.mark.django_db
def test_competitor_add_manual_instagram_batch_mixed_success_and_failures(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=229, tg_chat_id=229)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)

    def fake_resolve_instagram_seeds_batch(raw_inputs, *, context=None):
        assert context is not None
        return [
            SeedResolution(
                platform=Platform.INSTAGRAM,
                external_id="ig-mixed-1",
                handle="mixed_1",
                url="https://www.instagram.com/mixed_1/",
                title="Mixed 1",
                description=None,
                uploads_playlist_id=None,
            ),
            None,
            SeedResolution(
                platform=Platform.INSTAGRAM,
                external_id="ig-mixed-3",
                handle="mixed_3",
                url="https://www.instagram.com/mixed_3/",
                title="Mixed 3",
                description=None,
                uploads_playlist_id=None,
            ),
            None,
        ]

    monkeypatch.setattr(competitors, "resolve_instagram_seeds_batch", fake_resolve_instagram_seeds_batch)
    monkeypatch.setattr(
        competitors,
        "resolve_exact_seed",
        lambda raw_input, *, context=None: pytest.fail(f"resolve_exact_seed should not run for pure IG batch: {raw_input}"),
    )

    state = DummyState()
    async_to_sync(state.update_data)(user_id=user.id)
    async_to_sync(state.set_state)(CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT)
    message = DummyMessage(
        user_id=229,
        text="\n".join(
            [
                "https://www.instagram.com/mixed.one/?igsh=1",
                "https://www.instagram.com/mixed.two/?igsh=2",
                "https://www.instagram.com/mixed.three/?igsh=3",
                "https://www.instagram.com/mixed.four/?igsh=4",
            ]
        ),
    )

    async_to_sync(competitors.on_competitor_add_manual_input)(message, state)

    count = async_to_sync(sync_to_async(UserCompetitor.objects.filter(user=user, is_active=True).count, thread_sensitive=True))()
    assert state.state is None
    assert count == 2
    assert "Добавлено вручную: 2" in message.answers[-1]
    assert "Ошибки: 2" in message.answers[-1]
    assert "Instagram: 2" in message.answers[-1]
    assert "https://www.instagram.com/mixed.two/?igsh=2: не смог подтвердить профиль" in message.answers[-1]
    assert "https://www.instagram.com/mixed.four/?igsh=4: не смог подтвердить профиль" in message.answers[-1]


@pytest.mark.django_db
def test_competitor_add_manual_shows_visible_failure_message_on_unexpected_exception(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=230, tg_chat_id=230)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])

    monkeypatch.setattr(competitors, "db_call", _db_call)

    async def failing_db_run(func, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(competitors, "db_run", failing_db_run)

    state = DummyState()
    async_to_sync(state.update_data)(user_id=user.id)
    async_to_sync(state.set_state)(CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT)
    message = DummyMessage(user_id=230, text="https://www.instagram.com/failure.case/?igsh=fail")

    async_to_sync(competitors.on_competitor_add_manual_input)(message, state)

    assert state.state is None
    assert "Проверяю профили..." in message.answers[0]
    assert "Не удалось обработать список профилей. Попробуй еще раз позже." in message.answers[-1]


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
def test_competitors_remove_picker_pages_and_done_preserve_selection(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=131, tg_chat_id=131)
    async_to_sync(sync_to_async(Schedule.objects.create, thread_sensitive=True))(user=user, times=["09:00"])
    competitor_ids: list[int] = []
    for idx in range(9):
        competitor_obj = async_to_sync(sync_to_async(Competitor.objects.create, thread_sensitive=True))(
            platform=Platform.YOUTUBE,
            external_id=f"yt-remove-page-{idx}",
            handle=f"remove_page_{idx}",
            display_name=f"Remove Page {idx}",
            url=f"https://www.youtube.com/@remove_page_{idx}",
        )
        async_to_sync(sync_to_async(UserCompetitor.objects.create, thread_sensitive=True))(
            user=user,
            competitor=competitor_obj,
            is_active=True,
        )
        competitor_ids.append(competitor_obj.id)

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)

    state = DummyState()
    message = DummyMessage(user_id=131)
    async_to_sync(competitors.cmd_competitors_remove)(message, state)

    async_to_sync(competitors.on_remove_page)(DummyCallbackQuery(data="comprem_page:1", message=message), state)
    async_to_sync(competitors.on_remove_toggle)(DummyCallbackQuery(data=f"comprem_toggle:{competitor_ids[-1]}", message=message), state)
    async_to_sync(competitors.on_remove_page)(DummyCallbackQuery(data="comprem_page:0", message=message), state)
    async_to_sync(competitors.on_remove_done)(DummyCallbackQuery(data="comprem_done", message=message), state)

    removed_link = async_to_sync(sync_to_async(UserCompetitor.objects.get, thread_sensitive=True))(
        user=user,
        competitor_id=competitor_ids[-1],
    )
    active_count = async_to_sync(sync_to_async(UserCompetitor.objects.filter(user=user, is_active=True).count, thread_sensitive=True))()

    assert state.state is None
    assert removed_link.is_active is False
    assert active_count == 8
    assert message.edit_reply_markup_calls >= 2
    assert "Удалено: 1" in message.answers[-1]
    assert "YouTube: 8" in message.answers[-1]


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
    report = async_to_sync(sync_to_async(Report.objects.get, thread_sensitive=True))(id=report.id)
    acceptance = report.payload["suggested_competitors"]["youtube"]["acceptance"]
    assert link.is_active is True
    assert link.added_by == "suggested"
    assert acceptance["clicked_add"] == 1
    assert acceptance["added"] == 1
    assert acceptance["already_active"] == 0
    assert acceptance["events"][0]["status"] == "reactivated"
    assert acceptance["events"][0]["suggestion_source"] == ""
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
    report = async_to_sync(sync_to_async(Report.objects.get, thread_sensitive=True))(id=report.id)
    acceptance = report.payload["suggested_competitors"]["youtube"]["acceptance"]
    link = async_to_sync(
        sync_to_async(
            UserCompetitor.objects.select_related("competitor").get,
            thread_sensitive=True,
        )
    )(user=user, competitor__external_id="yt-suggested-21")
    assert count == 21
    assert link.added_by == "suggested"
    assert acceptance["clicked_add"] == 2
    assert acceptance["added"] == 1
    assert acceptance["already_active"] == 1
    assert [event["status"] for event in acceptance["events"]] == ["added", "already_active"]
    assert acceptance["events"][0]["channel_id"] == "yt-suggested-21"
    assert any("Активных YouTube-конкурентов: 21" in answer for answer in message.answers)
    assert message.answers[-1] == "Suggested 21 уже есть в активных YouTube-конкурентах."


@pytest.mark.django_db
def test_suggested_instagram_add_reactivates_competitor(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=333, tg_chat_id=333)
    competitor_obj = async_to_sync(sync_to_async(Competitor.objects.create, thread_sensitive=True))(
        platform=Platform.INSTAGRAM,
        external_id="ig-suggested-reactivate",
        handle="ig_reactivate",
        display_name="IG Suggested Reactivate",
        url="https://www.instagram.com/ig_reactivate/",
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
                "instagram": {
                    "items": [
                        {
                            "competitor_external_id": "ig-suggested-reactivate",
                            "competitor_display_name": "IG Suggested Reactivate",
                            "competitor_handle": "ig_reactivate",
                            "competitor_url": "https://www.instagram.com/ig_reactivate/",
                            "appearance_count": 2,
                            "suggestion_reason": "repeated_report_competitor",
                        }
                    ]
                }
            },
        },
    )

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)

    message = DummyMessage(user_id=333)
    callback = DummyCallbackQuery(data=f"sugigadd:{report.id}:0", message=message)
    async_to_sync(competitors.on_suggested_instagram_add)(callback)

    link = async_to_sync(sync_to_async(UserCompetitor.objects.get, thread_sensitive=True))(user=user, competitor=competitor_obj)
    report = async_to_sync(sync_to_async(Report.objects.get, thread_sensitive=True))(id=report.id)
    acceptance = report.payload["suggested_competitors"]["instagram"]["acceptance"]
    assert link.is_active is True
    assert link.added_by == "suggested"
    assert acceptance["clicked_add"] == 1
    assert acceptance["added"] == 1
    assert acceptance["already_active"] == 0
    assert acceptance["events"][0]["status"] == "reactivated"
    assert acceptance["events"][0]["suggestion_source"] == ""
    assert "Добавил конкурента в Instagram: IG Suggested Reactivate." in message.answers[-1]


@pytest.mark.django_db
def test_suggested_instagram_add_is_idempotent(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=334, tg_chat_id=334)
    report = async_to_sync(sync_to_async(Report.objects.create, thread_sensitive=True))(
        user=user,
        period_start=datetime(2026, 3, 30, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 31, 0, 0, tzinfo=UTC),
        status="sent",
        payload={
            "sections": [],
            "suggested_competitors": {
                "instagram": {
                    "items": [
                        {
                            "competitor_external_id": "ig-suggested-1",
                            "competitor_display_name": "IG Suggested 1",
                            "competitor_handle": "ig_suggested_1",
                            "competitor_url": "https://www.instagram.com/ig_suggested_1/",
                            "appearance_count": 3,
                            "suggestion_reason": "repeated_report_competitor",
                        }
                    ]
                }
            },
        },
    )

    monkeypatch.setattr(competitors, "db_call", _db_call)
    monkeypatch.setattr(competitors, "db_run", _db_run)

    message = DummyMessage(user_id=334)
    callback = DummyCallbackQuery(data=f"sugigadd:{report.id}:0", message=message)
    async_to_sync(competitors.on_suggested_instagram_add)(callback)
    async_to_sync(competitors.on_suggested_instagram_add)(callback)

    count = async_to_sync(sync_to_async(UserCompetitor.objects.filter(user=user, is_active=True).count, thread_sensitive=True))()
    link = async_to_sync(
        sync_to_async(
            UserCompetitor.objects.select_related("competitor").get,
            thread_sensitive=True,
        )
    )(user=user, competitor__external_id="ig-suggested-1")
    report = async_to_sync(sync_to_async(Report.objects.get, thread_sensitive=True))(id=report.id)
    acceptance = report.payload["suggested_competitors"]["instagram"]["acceptance"]

    assert count == 1
    assert link.added_by == "suggested"
    assert acceptance["clicked_add"] == 2
    assert acceptance["added"] == 1
    assert acceptance["already_active"] == 1
    assert [event["status"] for event in acceptance["events"]] == ["added", "already_active"]
    assert any("Активных Instagram-конкурентов: 1" in answer for answer in message.answers)
    assert message.answers[-1] == "IG Suggested 1 уже есть в активных Instagram-конкурентах."
