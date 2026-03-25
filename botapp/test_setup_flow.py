from __future__ import annotations

from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync, sync_to_async

from botapp.handlers import setup
from botapp.state import SetupStates
from tracking.adapters.base import SeedResolution
from tracking.models import SeedProfile, SeedStatus, TgUser
from tracking.services.account_linking import LinkedAccountSuggestion
from tracking.services.platform_onboarding import DiscoveryOutcome, PlatformDiscoveryStatus


class DummyState:
    def __init__(self, data: dict):
        self.data = dict(data)
        self.state = None

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def set_state(self, value) -> None:
        self.state = value


class DummyMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.chat = SimpleNamespace(id=1)
        self.bot = SimpleNamespace()

    async def answer(self, text: str, reply_markup=None):
        self.answers.append(text)
        return SimpleNamespace(chat=self.chat, message_id=len(self.answers), bot=self.bot)


async def _db_call(func, *args, **kwargs):
    return await sync_to_async(func, thread_sensitive=True)(*args, **kwargs)


async def _db_run(func, *args, **kwargs):
    return await sync_to_async(func, thread_sensitive=True)(*args, **kwargs)


@pytest.mark.django_db
def test_start_keywords_step_uses_instagram_seed_without_manual_prompt(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=101, tg_chat_id=101)
    seed_profile = async_to_sync(sync_to_async(SeedProfile.objects.create, thread_sensitive=True))(
        user=user,
        raw_input="https://www.instagram.com/nasa/",
        detected_platform="instagram",
        canonical_url="https://www.instagram.com/nasa/",
        niche_keywords=[],
        niche_source="manual",
        status=SeedStatus.PENDING,
    )
    state = DummyState(
        {
            "user_id": user.id,
            "seed_profile_id": seed_profile.id,
            "seed": {
                "platform": "instagram",
                "external_id": "ig-1",
                "handle": "nasa",
                "url": "https://www.instagram.com/nasa/",
                "title": "NASA",
                "description": "Space agency",
                "uploads_playlist_id": None,
            },
            "competitor_seeds": [],
        }
    )
    message = DummyMessage()
    shown = {}

    monkeypatch.setattr(setup, "db_call", _db_call)
    monkeypatch.setattr(setup, "db_run", _db_run)
    monkeypatch.setattr(
        setup,
        "decide_and_consume_llm_call",
        lambda **kwargs: SimpleNamespace(allow=False, reason="disabled", used_today=0, max_calls_per_day=0),
    )
    monkeypatch.setattr(setup, "infer_niche_keywords", lambda **kwargs: (["space", "mars"], "auto"))

    async def fake_show_keywords_editor(message, state):
        shown["called"] = True
        await state.set_state(SetupStates.EDIT_NICHE)

    monkeypatch.setattr(setup, "_show_keywords_editor", fake_show_keywords_editor)

    async_to_sync(setup._start_keywords_step)(message, state)

    assert shown == {"called": True}
    assert state.state == SetupStates.EDIT_NICHE
    assert state.data["niche_keywords"] == ["space", "mars"]
    assert all("Пришли ключевые слова" not in text for text in message.answers)


@pytest.mark.django_db
def test_start_keywords_step_uses_tiktok_seed_without_manual_prompt(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=202, tg_chat_id=202)
    seed_profile = async_to_sync(sync_to_async(SeedProfile.objects.create, thread_sensitive=True))(
        user=user,
        raw_input="https://www.tiktok.com/@nba",
        detected_platform="tiktok",
        canonical_url="https://www.tiktok.com/@nba",
        niche_keywords=[],
        niche_source="manual",
        status=SeedStatus.PENDING,
    )
    state = DummyState(
        {
            "user_id": user.id,
            "seed_profile_id": seed_profile.id,
            "seed": {
                "platform": "tiktok",
                "external_id": "tt-1",
                "handle": "nba",
                "url": "https://www.tiktok.com/@nba",
                "title": "NBA",
                "description": "Basketball league",
                "uploads_playlist_id": None,
            },
            "competitor_seeds": [],
        }
    )
    message = DummyMessage()
    shown = {}

    monkeypatch.setattr(setup, "db_call", _db_call)
    monkeypatch.setattr(setup, "db_run", _db_run)
    monkeypatch.setattr(
        setup,
        "decide_and_consume_llm_call",
        lambda **kwargs: SimpleNamespace(allow=False, reason="disabled", used_today=0, max_calls_per_day=0),
    )
    monkeypatch.setattr(setup, "infer_niche_keywords", lambda **kwargs: (["basketball", "highlights"], "auto"))

    async def fake_show_keywords_editor(message, state):
        shown["called"] = True
        await state.set_state(SetupStates.EDIT_NICHE)

    monkeypatch.setattr(setup, "_show_keywords_editor", fake_show_keywords_editor)

    async_to_sync(setup._start_keywords_step)(message, state)

    assert shown == {"called": True}
    assert state.state == SetupStates.EDIT_NICHE
    assert state.data["niche_keywords"] == ["basketball", "highlights"]
    assert all("Пришли ключевые слова" not in text for text in message.answers)


def test_begin_account_linking_prompts_confirmation(monkeypatch):
    state = DummyState({"user_id": 1})
    message = DummyMessage()
    seed = SeedResolution(
        platform="youtube",
        external_id="yt-1",
        handle="creator",
        url="https://www.youtube.com/@creator",
        title="Creator",
        description="Creator channel",
        uploads_playlist_id="UU123",
    )

    monkeypatch.setattr(
        setup,
        "suggest_accounts_for_platforms",
        lambda **kwargs: {
            "tiktok": LinkedAccountSuggestion(
                platform="tiktok",
                candidates=[
                    SimpleNamespace(
                        seed=SeedResolution(
                            platform="tiktok",
                            external_id="tt-1",
                            handle="creator",
                            url="https://www.tiktok.com/@creator",
                            title="Creator",
                            description="Creator profile",
                            uploads_playlist_id=None,
                        ),
                        signals=["exact_handle", "display_similarity:1.00"],
                        score=125,
                    )
                ],
                note=None,
            ),
            "instagram": LinkedAccountSuggestion(platform="instagram", candidates=[], note="none"),
        },
    )

    async_to_sync(setup._begin_account_linking)(message, state, seed_profile_id=11, seed=seed)

    assert state.state == SetupStates.PICK_LINKED_ACCOUNT
    assert state.data["current_link_platform"] == "tiktok"
    assert "Это ваш" in message.answers[-1]
    assert "TikTok" in message.answers[-1]


def test_manual_link_input_updates_linked_accounts(monkeypatch):
    state = DummyState(
        {
            "user_id": 1,
            "current_link_platform": "instagram",
            "link_platform_queue": ["instagram"],
            "linked_accounts": {
                "youtube": {
                    "platform": "youtube",
                    "external_id": "yt-1",
                    "handle": "creator",
                    "url": "https://www.youtube.com/@creator",
                    "title": "Creator",
                    "source": "seed",
                    "signals": ["seed_exact_resolve"],
                    "is_seed": True,
                }
            },
        }
    )
    message = DummyMessage()
    message.text = "https://www.instagram.com/creator/"

    monkeypatch.setattr(
        setup,
        "resolve_seed_for_platform",
        lambda **kwargs: SeedResolution(
            platform="instagram",
            external_id="ig-1",
            handle="creator",
            url="https://www.instagram.com/creator/",
            title="Creator",
            description="Creator profile",
            uploads_playlist_id=None,
        ),
    )
    called = {}

    async def fake_ask_next_linked_account(message, state):
        called["called"] = True

    monkeypatch.setattr(setup, "_ask_next_linked_account", fake_ask_next_linked_account)

    async_to_sync(setup.on_link_manual_input)(message, state)

    assert called == {"called": True}
    assert state.data["link_platform_queue"] == []
    assert state.data["linked_accounts"]["instagram"]["external_id"] == "ig-1"
    assert state.data["linked_accounts"]["instagram"]["signals"] == ["manual_input"]


@pytest.mark.django_db
def test_start_keywords_step_passes_confirmed_linked_accounts(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=303, tg_chat_id=303)
    seed_profile = async_to_sync(sync_to_async(SeedProfile.objects.create, thread_sensitive=True))(
        user=user,
        raw_input="https://www.youtube.com/@nasa",
        detected_platform="youtube",
        canonical_url="https://www.youtube.com/@nasa",
        niche_keywords=[],
        niche_source="manual",
        status=SeedStatus.PENDING,
    )
    state = DummyState(
        {
            "user_id": user.id,
            "seed_profile_id": seed_profile.id,
            "seed": {
                "platform": "youtube",
                "external_id": "yt-1",
                "handle": "nasa",
                "url": "https://www.youtube.com/@nasa",
                "title": "NASA",
                "description": "Space exploration",
                "uploads_playlist_id": "UU123",
            },
            "linked_accounts": {
                "youtube": {
                    "platform": "youtube",
                    "external_id": "yt-1",
                    "handle": "nasa",
                    "url": "https://www.youtube.com/@nasa",
                    "title": "NASA",
                    "description": "Space exploration",
                    "uploads_playlist_id": "UU123",
                },
                "instagram": {
                    "platform": "instagram",
                    "external_id": "ig-1",
                    "handle": "nasa",
                    "url": "https://www.instagram.com/nasa/",
                    "title": "NASA Instagram",
                    "description": "Space photography",
                    "uploads_playlist_id": None,
                },
            },
            "competitor_seeds": [],
        }
    )
    message = DummyMessage()
    captured = {}

    monkeypatch.setattr(setup, "db_call", _db_call)
    monkeypatch.setattr(setup, "db_run", _db_run)
    monkeypatch.setattr(
        setup,
        "decide_and_consume_llm_call",
        lambda **kwargs: SimpleNamespace(allow=False, reason="disabled", used_today=0, max_calls_per_day=0),
    )

    def fake_infer_niche_keywords(**kwargs):
        captured["linked_accounts"] = kwargs.get("linked_accounts")
        return ["space", "science"], "auto"

    monkeypatch.setattr(setup, "infer_niche_keywords", fake_infer_niche_keywords)

    async def fake_show_keywords_editor(message, state):
        await state.set_state(SetupStates.EDIT_NICHE)

    monkeypatch.setattr(setup, "_show_keywords_editor", fake_show_keywords_editor)

    async_to_sync(setup._start_keywords_step)(message, state)

    assert state.state == SetupStates.EDIT_NICHE
    assert [seed.platform for seed in captured["linked_accounts"]] == ["youtube", "instagram"]


@pytest.mark.django_db
def test_start_discovery_reports_per_platform_statuses_without_vague_failure(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(tg_user_id=404, tg_chat_id=404)
    state = DummyState(
        {
            "user_id": user.id,
            "seed": {
                "platform": "instagram",
                "external_id": "ig-1",
                "handle": "creator",
                "url": "https://www.instagram.com/creator/",
                "title": "Creator",
                "description": "English teaching",
                "uploads_playlist_id": None,
            },
            "niche_keywords": ["english teachers", "lesson plans"],
            "competitor_seeds": [],
        }
    )
    message = DummyMessage()
    called = {}

    monkeypatch.setattr(setup, "db_call", _db_call)
    monkeypatch.setattr(
        setup,
        "_load_confirmed_linked_accounts",
        lambda **kwargs: sync_to_async(
            lambda: [
                SeedResolution(
                    platform="instagram",
                    external_id="ig-1",
                    handle="creator",
                    url="https://www.instagram.com/creator/",
                    title="Creator",
                    description="English teaching",
                    uploads_playlist_id=None,
                )
            ],
            thread_sensitive=True,
        )(),
    )
    monkeypatch.setattr(
        setup,
        "discover_competitors_for_onboarding",
        lambda **kwargs: DiscoveryOutcome(
            candidates=[],
            platform_statuses=[
                PlatformDiscoveryStatus(platform="youtube", status="EMPTY", reason="по текущим ключевым фразам кандидаты не найдены."),
                PlatformDiscoveryStatus(platform="tiktok", status="EMPTY", reason="текущий провайдер не отдает связанные профили, поэтому автоподбор пока недоступен."),
                PlatformDiscoveryStatus(platform="instagram", status="ERROR", reason="provider timeout"),
            ],
            notes=[
                "YouTube: EMPTY — по текущим ключевым фразам кандидаты не найдены.",
                "TikTok: EMPTY — текущий провайдер не отдает связанные профили, поэтому автоподбор пока недоступен.",
                "Instagram: ERROR — provider timeout",
            ],
        ),
    )

    async def fake_ask_timezone_method(message, state):
        called["timezone"] = True

    monkeypatch.setattr(setup, "_ask_timezone_method", fake_ask_timezone_method)

    async_to_sync(setup._start_discovery)(message, state)

    assert called == {"timezone": True}
    assert message.answers[0] == "Подбираю конкурентов по платформам…"
    assert "Результат автоподбора по платформам:" in message.answers[-1]
    assert "YouTube: EMPTY" in message.answers[-1]
    assert "TikTok: EMPTY" in message.answers[-1]
    assert "Instagram: ERROR — provider timeout" in message.answers[-1]
    assert "Автоподбор не сработал:" not in message.answers[-1]
