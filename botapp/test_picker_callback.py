from __future__ import annotations

import logging
from types import SimpleNamespace

from asgiref.sync import async_to_sync

from botapp.picker_callback import (
    handle_picker_page_callback,
    handle_picker_select_all_callback,
    handle_picker_toggle_callback,
)
from botapp.picker_session import PickerSessionKeys


class DummyState:
    def __init__(self) -> None:
        self.data: dict = {}

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self.data)


class DummyMessage:
    def __init__(self, *, user_id: int) -> None:
        self.chat = SimpleNamespace(id=user_id)
        self.from_user = SimpleNamespace(id=user_id)
        self.reply_markups = []
        self.edit_text_calls = 0
        self.edit_reply_markup_calls = 0

    async def edit_reply_markup(self, reply_markup=None):
        self.edit_reply_markup_calls += 1
        self.reply_markups.append(reply_markup)
        return self

    async def edit_text(self, text, reply_markup=None):
        self.edit_text_calls += 1
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


def test_handle_picker_page_callback_updates_page_and_renders_markup():
    state = DummyState()
    state.data = {"page": 0, "selected": []}
    keys = PickerSessionKeys(page_key="page", selected_key="selected")
    message = DummyMessage(user_id=1)
    callback = DummyCallbackQuery(data="page:9", message=message)
    render_calls: list[tuple[int, str, str]] = []

    async def render(message, state, *, data, edit_mode, callback_info):
        render_calls.append((data["page"], edit_mode, callback_info["ack"].status))

    async_to_sync(handle_picker_page_callback)(
        cb=callback,
        state=state,
        logger=logging.getLogger("botapp.test_picker_callback"),
        callback_type="test_page",
        session_keys=keys,
        requested_page=9,
        total_count=lambda data: 12,
        page_size=5,
        render=render,
    )

    assert state.data["page"] == 2
    assert render_calls == [(2, "markup", "success")]


def test_handle_picker_toggle_callback_updates_selection_and_renders():
    state = DummyState()
    state.data = {"page": 1, "selected": [3, 1]}
    keys = PickerSessionKeys(page_key="page", selected_key="selected")
    message = DummyMessage(user_id=2)
    callback = DummyCallbackQuery(data="toggle:2", message=message)
    render_calls: list[tuple[list[int], int]] = []

    async def render(message, state, *, data, edit_mode, callback_info):
        render_calls.append((data["selected"], callback_info["page_before"]))

    async_to_sync(handle_picker_toggle_callback)(
        cb=callback,
        state=state,
        logger=logging.getLogger("botapp.test_picker_callback"),
        callback_type="test_toggle",
        session_keys=keys,
        item_id=2,
        render=render,
    )

    assert state.data["selected"] == [1, 2, 3]
    assert render_calls == [([1, 2, 3], 1)]


def test_handle_picker_select_all_callback_updates_selection_and_tolerates_failed_ack():
    state = DummyState()
    state.data = {"page": 2, "selected": []}
    keys = PickerSessionKeys(page_key="page", selected_key="selected")
    message = DummyMessage(user_id=3)
    callback = DummyCallbackQuery(data="all", message=message, fail_answer=True)
    render_calls: list[tuple[list[int], str]] = []

    async def render(message, state, *, data, edit_mode, callback_info):
        render_calls.append((data["selected"], callback_info["ack"].status))

    async_to_sync(handle_picker_select_all_callback)(
        cb=callback,
        state=state,
        logger=logging.getLogger("botapp.test_picker_callback"),
        callback_type="test_all",
        session_keys=keys,
        selected_ids=lambda data: [4, 2, 2, 0],
        render=render,
    )

    assert state.data["selected"] == [0, 2, 4]
    assert render_calls == [([0, 2, 4], "failure:RuntimeError")]
