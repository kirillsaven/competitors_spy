from __future__ import annotations

from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync, sync_to_async

from botapp.handlers import stopwords
from botapp.state import StopwordManagementStates
from tracking.models import TgUser
from tracking.services.picker_snapshot_cache import (
    PICKER_SNAPSHOT_CACHE_SOURCE_COLD_BUILD,
    PICKER_SNAPSHOT_CACHE_SOURCE_SNAPSHOT_HIT,
    load_stopword_add_picker_snapshot,
    load_stopword_remove_picker_snapshot,
)
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
        self.edit_text_calls = 0
        self.edit_reply_markup_calls = 0
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


def _button_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


@pytest.mark.django_db
def test_stopwords_command_shows_current_list(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(
        tg_user_id=41,
        tg_chat_id=41,
        report_stopwords=["spoiler", "promo post"],
    )

    monkeypatch.setattr(stopwords, "db_call", _db_call)
    monkeypatch.setattr(stopwords, "db_run", _db_run)

    message = DummyMessage(user_id=41)
    async_to_sync(stopwords.cmd_stopwords)(message)

    assert "Стоп-слова отчета:" in message.answers[-1]
    assert "1. spoiler" in message.answers[-1]
    assert "2. promo post" in message.answers[-1]
    assert "/stopwords_add - выбрать и добавить" in message.answers[-1]
    assert "/stopwords_remove - выбрать и удалить" in message.answers[-1]
    assert user.report_stopwords == ["spoiler", "promo post"]


@pytest.mark.django_db
def test_stopwords_add_uses_picker_and_skips_existing(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(
        tg_user_id=42,
        tg_chat_id=42,
        report_stopwords=["spoiler"],
    )

    monkeypatch.setattr(stopwords, "db_call", _db_call)
    monkeypatch.setattr(stopwords, "db_run", _db_run)
    monkeypatch.setattr(stopwords, "build_user_stopword_suggestions", lambda **kwargs: ["promo post", "spoiler"])

    state = DummyState()
    message = DummyMessage(user_id=42)
    async_to_sync(stopwords.cmd_stopwords_add)(message, state)
    assert state.state == StopwordManagementStates.PICK_STOPWORDS_ADD

    toggle_first = DummyCallbackQuery(data="stopadd_toggle:0", message=message)
    async_to_sync(stopwords.on_add_toggle)(toggle_first, state)
    toggle_second = DummyCallbackQuery(data="stopadd_toggle:1", message=message)
    async_to_sync(stopwords.on_add_toggle)(toggle_second, state)
    done = DummyCallbackQuery(data="stopadd_done", message=message)
    async_to_sync(stopwords.on_add_done)(done, state)

    updated = async_to_sync(sync_to_async(TgUser.objects.get, thread_sensitive=True))(id=user.id)
    assert state.state is None
    assert updated.report_stopwords == ["spoiler", "promo post"]
    assert "Добавлено: 1" in message.answers[-1]
    assert "Пропущено: 1" in message.answers[-1]
    assert "Всего стоп-слов: 2" in message.answers[-1]


def test_stopwords_add_page_failed_ack_does_not_abort_render():
    suggestions = [f"word {idx}" for idx in range(12)]
    state = DummyState()
    async_to_sync(state.update_data)(
        user_id=142,
        stopword_add_candidates=suggestions,
        stopword_add_selected_ids=[],
        stopword_add_page=0,
    )
    message = DummyMessage(user_id=142)

    callback = DummyCallbackQuery(data="stopadd_page:1", message=message, fail_answer=True)
    async_to_sync(stopwords.on_add_page)(callback, state)

    assert state.data["stopword_add_page"] == 1
    assert message.edit_text_calls == 0
    assert message.edit_reply_markup_calls == 1
    assert any("word 10" in text for text in _button_texts(message.reply_markups[-1]))


def test_stopwords_add_toggle_uses_markup_only():
    suggestions = [f"word {idx}" for idx in range(12)]
    state = DummyState()
    async_to_sync(state.update_data)(
        user_id=144,
        stopword_add_candidates=suggestions,
        stopword_add_selected_ids=[],
        stopword_add_page=0,
    )
    message = DummyMessage(user_id=144)

    async_to_sync(stopwords.on_add_toggle)(DummyCallbackQuery(data="stopadd_toggle:0", message=message), state)

    assert state.data["stopword_add_selected_ids"] == [0]
    assert message.edit_text_calls == 0
    assert message.edit_reply_markup_calls == 1
    assert any(text.startswith("✅ word 0") for text in _button_texts(message.reply_markups[-1]))
    assert "Добавить (1)" in _button_texts(message.reply_markups[-1])


@pytest.mark.django_db
def test_stopwords_add_cache_hit_uses_single_send(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(
        tg_user_id=148,
        tg_chat_id=148,
        report_stopwords=["spoiler"],
    )

    monkeypatch.setattr(stopwords, "db_call", _db_call)
    monkeypatch.setattr(stopwords, "_peek_stopword_add_picker_snapshot_cached", lambda **kwargs: SimpleNamespace(
        payload={"items": ["promo post", "launch teaser"]},
        cache_hit=True,
        cache_source=PICKER_SNAPSHOT_CACHE_SOURCE_SNAPSHOT_HIT,
        build_ms=0.0,
    ))
    monkeypatch.setattr(
        stopwords,
        "_load_stopword_add_picker_snapshot_cached",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("cold load should not run")),
    )

    state = DummyState()
    message = DummyMessage(user_id=148)
    async_to_sync(stopwords.cmd_stopwords_add)(message, state)

    assert state.state == StopwordManagementStates.PICK_STOPWORDS_ADD
    assert len(message.answers) == 1
    assert message.answers[0].startswith("Выбери фразы")
    assert message.edit_text_calls == 0
    assert message.edit_reply_markup_calls == 0
    assert message.reply_markups[0] is not None


@pytest.mark.django_db
def test_stopwords_add_second_open_reuses_snapshot_cache():
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(
        tg_user_id=145,
        tg_chat_id=145,
        report_stopwords=["spoiler"],
    )
    calls = {"count": 0}

    def fake_builder(*, user):
        calls["count"] += 1
        return ["promo post", "launch teaser"]

    clear_retry_cache()
    first = load_stopword_add_picker_snapshot(user=user, builder=fake_builder)
    second = load_stopword_add_picker_snapshot(user=user, builder=fake_builder)

    assert calls["count"] == 1
    assert first.cache_hit is False
    assert first.cache_source == PICKER_SNAPSHOT_CACHE_SOURCE_COLD_BUILD
    assert second.cache_hit is True
    assert second.cache_source == PICKER_SNAPSHOT_CACHE_SOURCE_SNAPSHOT_HIT
    assert first.payload == second.payload
    clear_retry_cache()


@pytest.mark.django_db
def test_stopwords_add_cold_open_keeps_placeholder_edit(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(
        tg_user_id=149,
        tg_chat_id=149,
        report_stopwords=["spoiler"],
    )

    monkeypatch.setattr(stopwords, "db_call", _db_call)
    monkeypatch.setattr(stopwords, "_peek_stopword_add_picker_snapshot_cached", lambda **kwargs: None)
    monkeypatch.setattr(stopwords, "_load_stopword_add_picker_snapshot_cached", lambda **kwargs: SimpleNamespace(
        payload={"items": ["promo post"]},
        cache_hit=False,
        cache_source=PICKER_SNAPSHOT_CACHE_SOURCE_COLD_BUILD,
        build_ms=10.0,
    ))

    state = DummyState()
    message = DummyMessage(user_id=149)
    async_to_sync(stopwords.cmd_stopwords_add)(message, state)

    assert state.state == StopwordManagementStates.PICK_STOPWORDS_ADD
    assert message.answers[0] == "Подбираю фразы для stopwords..."
    assert message.edit_text_calls == 1
    assert message.edit_reply_markup_calls == 0


@pytest.mark.django_db
def test_stopwords_add_snapshot_cache_invalidates_when_stopwords_change():
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(
        tg_user_id=146,
        tg_chat_id=146,
        report_stopwords=["spoiler"],
    )
    calls = {"count": 0}

    def fake_builder(*, user):
        calls["count"] += 1
        return [f"promo post {calls['count']}"]

    clear_retry_cache()
    first = load_stopword_add_picker_snapshot(user=user, builder=fake_builder)

    user.report_stopwords = ["spoiler", "launch"]
    async_to_sync(sync_to_async(user.save, thread_sensitive=True))(update_fields=["report_stopwords", "updated_at"])

    second = load_stopword_add_picker_snapshot(user=user, builder=fake_builder)

    assert calls["count"] == 2
    assert first.cache_hit is False
    assert second.cache_hit is False
    assert first.payload != second.payload
    clear_retry_cache()


def test_stopwords_add_selection_persists_across_pages():
    suggestions = [f"word {idx}" for idx in range(12)]
    state = DummyState()
    async_to_sync(state.update_data)(
        user_id=143,
        stopword_add_candidates=suggestions,
        stopword_add_selected_ids=[],
        stopword_add_page=0,
    )
    message = DummyMessage(user_id=143)

    async_to_sync(stopwords.on_add_toggle)(DummyCallbackQuery(data="stopadd_toggle:0", message=message), state)
    async_to_sync(stopwords.on_add_page)(DummyCallbackQuery(data="stopadd_page:1", message=message), state)
    async_to_sync(stopwords.on_add_toggle)(DummyCallbackQuery(data="stopadd_toggle:10", message=message), state)
    async_to_sync(stopwords.on_add_page)(DummyCallbackQuery(data="stopadd_page:0", message=message), state)

    assert state.data["stopword_add_selected_ids"] == [0, 10]
    assert state.data["stopword_add_page"] == 0
    assert message.edit_text_calls == 0
    assert message.edit_reply_markup_calls == 4
    assert any(text.startswith("✅ word 0") for text in _button_texts(message.reply_markups[-1]))
    assert "Добавить (2)" in _button_texts(message.reply_markups[-1])


@pytest.mark.django_db
def test_stopwords_remove_cache_hit_uses_single_send(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(
        tg_user_id=150,
        tg_chat_id=150,
        report_stopwords=["spoiler", "promo post"],
    )

    monkeypatch.setattr(stopwords, "db_call", _db_call)
    monkeypatch.setattr(stopwords, "_peek_stopword_remove_picker_snapshot_cached", lambda **kwargs: SimpleNamespace(
        payload={"items": ["spoiler", "promo post"]},
        cache_hit=True,
        cache_source=PICKER_SNAPSHOT_CACHE_SOURCE_SNAPSHOT_HIT,
        build_ms=0.0,
    ))
    monkeypatch.setattr(
        stopwords,
        "_load_stopword_remove_picker_snapshot_cached",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("cold load should not run")),
    )

    state = DummyState()
    message = DummyMessage(user_id=150)
    async_to_sync(stopwords.cmd_stopwords_remove)(message, state)

    assert state.state == StopwordManagementStates.PICK_STOPWORDS_REMOVE
    assert len(message.answers) == 1
    assert message.answers[0].startswith("Выбери stopwords")
    assert message.edit_text_calls == 0
    assert message.edit_reply_markup_calls == 0
    assert message.reply_markups[0] is not None


@pytest.mark.django_db
def test_stopwords_remove_second_open_reuses_snapshot_cache():
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(
        tg_user_id=147,
        tg_chat_id=147,
        report_stopwords=["spoiler", "promo post"],
    )

    clear_retry_cache()
    first = load_stopword_remove_picker_snapshot(user=user)
    second = load_stopword_remove_picker_snapshot(user=user)

    assert first.cache_hit is False
    assert first.cache_source == PICKER_SNAPSHOT_CACHE_SOURCE_COLD_BUILD
    assert second.cache_hit is True
    assert second.cache_source == PICKER_SNAPSHOT_CACHE_SOURCE_SNAPSHOT_HIT
    assert first.payload == second.payload
    clear_retry_cache()


@pytest.mark.django_db
def test_stopwords_remove_deletes_only_requested_user_words(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(
        tg_user_id=43,
        tg_chat_id=43,
        report_stopwords=["spoiler", "promo post", "launch"],
    )
    other_user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(
        tg_user_id=44,
        tg_chat_id=44,
        report_stopwords=["spoiler", "launch"],
    )

    monkeypatch.setattr(stopwords, "db_call", _db_call)
    monkeypatch.setattr(stopwords, "db_run", _db_run)

    state = DummyState()
    message = DummyMessage(user_id=43)
    async_to_sync(stopwords.cmd_stopwords_remove)(message, state)
    assert state.state == StopwordManagementStates.PICK_STOPWORDS_REMOVE

    toggle = DummyCallbackQuery(data="stoprem_toggle:0", message=message)
    async_to_sync(stopwords.on_remove_toggle)(toggle, state)
    done = DummyCallbackQuery(data="stoprem_done", message=message)
    async_to_sync(stopwords.on_remove_done)(done, state)

    updated = async_to_sync(sync_to_async(TgUser.objects.get, thread_sensitive=True))(id=user.id)
    untouched = async_to_sync(sync_to_async(TgUser.objects.get, thread_sensitive=True))(id=other_user.id)
    assert state.state is None
    assert updated.report_stopwords == ["promo post", "launch"]
    assert untouched.report_stopwords == ["spoiler", "launch"]
    assert "Удалено: 1" in message.answers[-1]
    assert "Пропущено: 0" in message.answers[-1]
    assert "Всего стоп-слов: 2" in message.answers[-1]
