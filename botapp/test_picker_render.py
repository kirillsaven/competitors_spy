from __future__ import annotations

from types import SimpleNamespace

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from asgiref.sync import async_to_sync

from botapp.picker_render import (
    PickerRenderView,
    build_picker_render_view,
    picker_body_text,
    picker_done_text,
    render_picker_message,
)


class DummyState:
    async def get_data(self) -> dict:
        return {}


class DummyMessage:
    def __init__(self) -> None:
        self.edit_text_calls = 0
        self.edit_reply_markup_calls = 0
        self.reply_markups = []

    async def edit_text(self, text: str, reply_markup=None):
        self.edit_text_calls += 1
        self.reply_markups.append(reply_markup)
        return self

    async def edit_reply_markup(self, reply_markup=None):
        self.edit_reply_markup_calls += 1
        self.reply_markups.append(reply_markup)
        return self


def test_build_picker_render_view_returns_expected_text_and_markup_shape():
    data = {"page": 99}
    body = picker_body_text(title="Заголовок", notes=["note 1"])

    view = build_picker_render_view(
        data=data,
        page_key="page",
        total_count=12,
        page_size=5,
        text=body,
        keyboard_builder=lambda page: InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"page={page}", callback_data="noop")],
                [InlineKeyboardButton(text=picker_done_text("Добавить", 2), callback_data="done")],
            ]
        ),
    )

    assert view.text == "Заголовок\n\nnote 1\nОтметки сохраняются при переключении страниц.\nКогда готово, нажми кнопку ниже."
    assert data["page"] == 2
    assert view.page == 2
    assert view.row_count == 12
    assert view.metrics.rows_count == 2
    assert view.metrics.rendered_button_count == 2
    assert view.reply_markup.inline_keyboard[1][0].text == "Добавить (2)"


def test_render_picker_message_passes_callback_info_to_logger():
    message = DummyMessage()
    callback_calls = []
    callback_info = {"ack": SimpleNamespace(status="success"), "page_before": 0}

    def build_view(data: dict) -> PickerRenderView:
        data["page"] = 1
        return PickerRenderView(
            text="body",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="noop")]]
            ),
            keyboard_render_ms=1.5,
            metrics=SimpleNamespace(rows_count=1, rendered_button_count=1),
            row_count=3,
            page=1,
        )

    def log_callback(**kwargs):
        callback_calls.append(kwargs)

    async_to_sync(render_picker_message)(
        message,
        DummyState(),
        data={"page": 0},
        edit_mode="markup",
        callback_info=callback_info,
        build_view=build_view,
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
        render_failure_event="picker_render_failed",
        log_callback=log_callback,
        page_key="page",
    )

    assert message.edit_text_calls == 0
    assert message.edit_reply_markup_calls == 1
    assert callback_calls[0]["page_before"] == 0
    assert callback_calls[0]["page_after"] == 1
    assert callback_calls[0]["candidate_count"] == 3
    assert callback_calls[0]["rows_count"] == 1
    assert callback_calls[0]["rendered_button_count"] == 1
