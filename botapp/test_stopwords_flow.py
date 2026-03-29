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
    assert "/stopwords_add - добавить" in message.answers[-1]
    assert "/stopwords_remove - удалить" in message.answers[-1]
    assert user.report_stopwords == ["spoiler", "promo post"]


@pytest.mark.django_db
def test_stopwords_add_normalizes_and_deduplicates(monkeypatch):
    user = async_to_sync(sync_to_async(TgUser.objects.create, thread_sensitive=True))(
        tg_user_id=42,
        tg_chat_id=42,
        report_stopwords=["spoiler"],
    )

    monkeypatch.setattr(stopwords, "db_call", _db_call)
    monkeypatch.setattr(stopwords, "db_run", _db_run)

    state = DummyState()
    command_message = DummyMessage(user_id=42)
    async_to_sync(stopwords.cmd_stopwords_add)(command_message, state)
    assert state.state == StopwordManagementStates.WAIT_STOPWORDS_ADD_INPUT

    input_message = DummyMessage(user_id=42, text="  Promo   Post,\nSPOILER,\nlaunch")
    async_to_sync(stopwords.on_stopwords_add_input)(input_message, state)

    updated = async_to_sync(sync_to_async(TgUser.objects.get, thread_sensitive=True))(id=user.id)
    assert state.state is None
    assert updated.report_stopwords == ["spoiler", "promo post", "launch"]
    assert "Добавлено: 2" in input_message.answers[-1]
    assert "Пропущено: 1" in input_message.answers[-1]
    assert "Всего стоп-слов: 3" in input_message.answers[-1]


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
    command_message = DummyMessage(user_id=43)
    async_to_sync(stopwords.cmd_stopwords_remove)(command_message, state)
    assert state.state == StopwordManagementStates.WAIT_STOPWORDS_REMOVE_INPUT

    input_message = DummyMessage(user_id=43, text="  SPOILER , missing ")
    async_to_sync(stopwords.on_stopwords_remove_input)(input_message, state)

    updated = async_to_sync(sync_to_async(TgUser.objects.get, thread_sensitive=True))(id=user.id)
    untouched = async_to_sync(sync_to_async(TgUser.objects.get, thread_sensitive=True))(id=other_user.id)
    assert state.state is None
    assert updated.report_stopwords == ["promo post", "launch"]
    assert untouched.report_stopwords == ["spoiler", "launch"]
    assert "Удалено: 1" in input_message.answers[-1]
    assert "Пропущено: 1" in input_message.answers[-1]
    assert "Всего стоп-слов: 2" in input_message.answers[-1]
