from __future__ import annotations

from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from botapp.handlers import setup
from botapp.state import SetupStates
from tracking.models import SeedProfile, SeedStatus, TgUser


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
@pytest.mark.asyncio
async def test_start_keywords_step_uses_instagram_seed_without_manual_prompt(monkeypatch):
    user = await sync_to_async(TgUser.objects.create, thread_sensitive=True)(tg_user_id=101, tg_chat_id=101)
    seed_profile = await sync_to_async(SeedProfile.objects.create, thread_sensitive=True)(
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

    await setup._start_keywords_step(message, state)

    assert shown == {"called": True}
    assert state.state == SetupStates.EDIT_NICHE
    assert state.data["niche_keywords"] == ["space", "mars"]
    assert all("Пришли ключевые слова" not in text for text in message.answers)


@pytest.mark.django_db
@pytest.mark.asyncio
async def test_start_keywords_step_uses_tiktok_seed_without_manual_prompt(monkeypatch):
    user = await sync_to_async(TgUser.objects.create, thread_sensitive=True)(tg_user_id=202, tg_chat_id=202)
    seed_profile = await sync_to_async(SeedProfile.objects.create, thread_sensitive=True)(
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

    await setup._start_keywords_step(message, state)

    assert shown == {"called": True}
    assert state.state == SetupStates.EDIT_NICHE
    assert state.data["niche_keywords"] == ["basketball", "highlights"]
    assert all("Пришли ключевые слова" not in text for text in message.answers)
