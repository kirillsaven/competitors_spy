from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from aiogram.types import InlineKeyboardMarkup, Message

from botapp.callback_safety import KeyboardMetrics, callback_started, keyboard_metrics, safe_edit_message
from botapp.picker_session import clamp_picker_page


@dataclass(frozen=True)
class PickerRenderView:
    text: str
    reply_markup: InlineKeyboardMarkup | None
    keyboard_render_ms: float
    metrics: KeyboardMetrics
    row_count: int
    page: int


def picker_body_text(*, title: str, notes: list[str] | None = None) -> str:
    lines = [title]
    extra = [str(item).strip() for item in (notes or []) if str(item).strip()]
    if extra:
        lines.append("")
        lines.extend(extra[:5])
    lines.append("Отметки сохраняются при переключении страниц.")
    lines.append("Когда готово, нажми кнопку ниже.")
    return "\n".join(lines)


def picker_done_text(action: str, selected_total: int) -> str:
    if selected_total <= 0:
        return action
    return f"{action} ({selected_total})"


def build_picker_render_view(
    *,
    data: dict,
    page_key: str,
    total_count: int,
    page_size: int,
    text: str,
    keyboard_builder: Callable[[int], InlineKeyboardMarkup | None],
) -> PickerRenderView:
    page = clamp_picker_page(int(data.get(page_key) or 0), total=total_count, page_size=page_size)
    keyboard_started = callback_started()
    reply_markup = keyboard_builder(page)
    keyboard_render_ms = (callback_started() - keyboard_started) * 1000
    metrics = keyboard_metrics(reply_markup)
    data[page_key] = page
    return PickerRenderView(
        text=text,
        reply_markup=reply_markup,
        keyboard_render_ms=keyboard_render_ms,
        metrics=metrics,
        row_count=total_count,
        page=page,
    )


async def render_picker_message(
    message: Message,
    state,
    *,
    data: dict | None,
    edit_mode: str,
    callback_info: dict | None,
    build_view: Callable[[dict], PickerRenderView],
    logger: logging.Logger,
    render_failure_event: str,
    log_callback: Callable[..., None],
    page_key: str,
) -> None:
    data = data if data is not None else await state.get_data()
    view = build_view(data)
    edit = await safe_edit_message(
        message,
        text=view.text,
        reply_markup=view.reply_markup,
        edit_mode=edit_mode,
    )
    if edit.failure_reason:
        logger.warning("%s edit_path=%s error=%s", render_failure_event, edit.path, edit.failure_reason)
    if callback_info:
        log_callback(
            ack=callback_info["ack"],
            edit=edit,
            page_before=int(callback_info.get("page_before") or 0),
            page_after=int(data.get(page_key) or 0),
            candidate_count=view.row_count,
            keyboard_render_ms=view.keyboard_render_ms,
            status="failure" if edit.failure_reason else "success",
            rows_count=view.metrics.rows_count,
            rendered_button_count=view.metrics.rendered_button_count,
        )
