from __future__ import annotations

from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync, sync_to_async

from botapp.handlers import stopwords
from botapp.state import StopwordManagementStates
from tracking.models import TgUser


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
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append((text, show_alert))


async def _db_call(func, *args, **kwargs):
    return await sync_to_async(func, thread_sensitive=True)(*args, **kwargs)


async def _db_run(func, *args, **kwargs):
    return await sync_to_async(func, thread_sensitive=True)(*args, **kwargs)


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
