from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from botapp.callback_safety import (
    CallbackAck,
    MessageEdit,
    callback_started,
    keyboard_metrics,
    log_callback_observability,
    safe_callback_ack,
    safe_edit_message,
)
from botapp.db import db_call, db_run
from botapp.keyboards import GLOBAL_BACK_CALLBACK, kb_manage_stopwords
from botapp.state import StopwordManagementStates
from botapp.user_sync import upsert_tg_user
from tracking.models import TgUser
from tracking.services.report_filters import get_user_report_stopwords
from tracking.services.stopword_suggestions import build_user_stopword_suggestions

router = Router()
logger = logging.getLogger(__name__)

_PAGE_SIZE = 10


def _save_user_stopwords(*, user: TgUser, stopwords: list[str]) -> None:
    user.report_stopwords = list(stopwords)
    user.save(update_fields=["report_stopwords", "updated_at"])


def _summary_lines(*, action: str, changed: int, skipped: int, total: int) -> str:
    return "\n".join(
        [
            f"{action}: {changed}",
            f"Пропущено: {skipped}",
            f"Всего стоп-слов: {total}",
        ]
    )


def _picker_text(*, title: str) -> str:
    return "\n".join(
        [
            title,
            "Отметки сохраняются при переключении страниц.",
            "Когда готово, нажми кнопку ниже.",
        ]
    )


def _picker_done_text(action: str, selected_total: int) -> str:
    if selected_total <= 0:
        return action
    return f"{action} ({selected_total})"


def _picker_page_count(total: int, page_size: int) -> int:
    return max(1, (max(0, total) + page_size - 1) // page_size)


def _clamp_picker_page(page: int, *, total: int, page_size: int) -> int:
    return min(max(0, page), _picker_page_count(total, page_size) - 1)


def _log_picker_callback(
    *,
    ack: CallbackAck,
    edit: MessageEdit | None = None,
    page_before: int,
    page_after: int,
    candidate_count: int,
    keyboard_render_ms: float,
    status: str,
    error: str | None = None,
    **fields,
) -> None:
    log_callback_observability(
        logger,
        event_name="stopword_picker_callback_observability",
        ack=ack,
        edit=edit,
        failure_reason=error,
        page_before=page_before,
        page_after=page_after,
        candidate_count=candidate_count,
        keyboard_render_ms=f"{keyboard_render_ms:.1f}",
        status=status,
        **fields,
    )


async def _render_add_picker(
    message: Message,
    state: FSMContext,
    *,
    data: dict | None = None,
    edit_mode: str = "text",
    callback_info: dict | None = None,
) -> None:
    data = data if data is not None else await state.get_data()
    suggestions = [str(item) for item in (data.get("stopword_add_candidates") or []) if str(item).strip()]
    selected = {int(item) for item in (data.get("stopword_add_selected_ids") or [])}
    page = _clamp_picker_page(int(data.get("stopword_add_page") or 0), total=len(suggestions), page_size=_PAGE_SIZE)
    text = _picker_text(
        title="Выбери фразы, которые нужно добавить в stopwords.",
    )
    keyboard_started = callback_started()
    kb = kb_manage_stopwords(
        stopwords=suggestions,
        selected_ids=selected,
        page=page,
        page_size=_PAGE_SIZE,
        toggle_prefix="stopadd_toggle",
        page_prefix="stopadd_page",
        all_callback="stopadd_all",
        done_callback="stopadd_done",
        done_text=_picker_done_text("Добавить", len(selected)),
    )
    keyboard_render_ms = (callback_started() - keyboard_started) * 1000
    metrics = keyboard_metrics(kb)
    edit = await safe_edit_message(message, text=text, reply_markup=kb, edit_mode=edit_mode)
    if edit.failure_reason:
        logger.warning("stopword_add_picker_render_failed edit_path=%s error=%s", edit.path, edit.failure_reason)
    if callback_info:
        _log_picker_callback(
            ack=callback_info["ack"],
            edit=edit,
            page_before=int(callback_info.get("page_before") or 0),
            page_after=page,
            candidate_count=len(suggestions),
            keyboard_render_ms=keyboard_render_ms,
            status="failure" if edit.failure_reason else "success",
            rows_count=metrics.rows_count,
            rendered_button_count=metrics.rendered_button_count,
        )


async def _render_remove_picker(
    message: Message,
    state: FSMContext,
    *,
    data: dict | None = None,
    edit_mode: str = "text",
    callback_info: dict | None = None,
) -> None:
    data = data if data is not None else await state.get_data()
    stopwords = [str(item) for item in (data.get("stopword_remove_items") or []) if str(item).strip()]
    selected = {int(item) for item in (data.get("stopword_remove_selected_ids") or [])}
    page = _clamp_picker_page(int(data.get("stopword_remove_page") or 0), total=len(stopwords), page_size=_PAGE_SIZE)
    text = _picker_text(
        title="Выбери stopwords, которые нужно удалить.",
    )
    keyboard_started = callback_started()
    kb = kb_manage_stopwords(
        stopwords=stopwords,
        selected_ids=selected,
        page=page,
        page_size=_PAGE_SIZE,
        toggle_prefix="stoprem_toggle",
        page_prefix="stoprem_page",
        all_callback="stoprem_all",
        done_callback="stoprem_done",
        done_text=_picker_done_text("Удалить", len(selected)),
    )
    keyboard_render_ms = (callback_started() - keyboard_started) * 1000
    metrics = keyboard_metrics(kb)
    edit = await safe_edit_message(message, text=text, reply_markup=kb, edit_mode=edit_mode)
    if edit.failure_reason:
        logger.warning("stopword_remove_picker_render_failed edit_path=%s error=%s", edit.path, edit.failure_reason)
    if callback_info:
        _log_picker_callback(
            ack=callback_info["ack"],
            edit=edit,
            page_before=int(callback_info.get("page_before") or 0),
            page_after=page,
            candidate_count=len(stopwords),
            keyboard_render_ms=keyboard_render_ms,
            status="failure" if edit.failure_reason else "success",
            rows_count=metrics.rows_count,
            rendered_button_count=metrics.rendered_button_count,
        )


@router.message(Command("stopwords"))
async def cmd_stopwords(message: Message) -> None:
    if not message.from_user:
        return
    user, _ = await db_call(
        upsert_tg_user,
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        language_code=message.from_user.language_code,
    )
    stopwords = await db_run(lambda: get_user_report_stopwords(user=user))
    if not stopwords:
        await message.answer(
            "Стоп-слов для отчетов пока нет.\n\n"
            "/stopwords_add - выбрать и добавить\n"
            "/stopwords_remove - выбрать и удалить"
        )
        return
    lines = ["Стоп-слова отчета:"]
    for index, word in enumerate(stopwords, start=1):
        lines.append(f"{index}. {word}")
    lines.append("")
    lines.append("/stopwords_add - выбрать и добавить")
    lines.append("/stopwords_remove - выбрать и удалить")
    await message.answer("\n".join(lines))


@router.message(Command("stopwords_add"))
async def cmd_stopwords_add(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    user, _ = await db_call(
        upsert_tg_user,
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        language_code=message.from_user.language_code,
    )
    loading = await message.answer("Подбираю фразы для stopwords...")
    try:
        suggestions = await asyncio.to_thread(build_user_stopword_suggestions, user=user)
    except Exception as exc:
        await state.clear()
        await loading.edit_text(f"Не смог подготовить список stopwords: {exc}")
        return
    if not suggestions:
        await state.clear()
        await loading.edit_text("Сейчас не нашел готовых фраз для добавления в stopwords.")
        return
    await state.update_data(
        user_id=user.id,
        stopword_add_candidates=suggestions,
        stopword_add_selected_ids=[],
        stopword_add_page=0,
    )
    await state.set_state(StopwordManagementStates.PICK_STOPWORDS_ADD)
    await _render_add_picker(loading, state)


@router.message(Command("stopwords_remove"))
async def cmd_stopwords_remove(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    user, _ = await db_call(
        upsert_tg_user,
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        language_code=message.from_user.language_code,
    )
    current = await db_run(lambda: get_user_report_stopwords(user=user))
    if not current:
        await state.clear()
        await message.answer("Стоп-слов для удаления сейчас нет.")
        return
    picker = await message.answer("Готовлю список stopwords...")
    await state.update_data(
        user_id=user.id,
        stopword_remove_items=current,
        stopword_remove_selected_ids=[],
        stopword_remove_page=0,
    )
    await state.set_state(StopwordManagementStates.PICK_STOPWORDS_REMOVE)
    await _render_remove_picker(picker, state)


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_ADD, F.data == "noop")
@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_REMOVE, F.data == "noop")
async def on_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_ADD, F.data == GLOBAL_BACK_CALLBACK)
@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_REMOVE, F.data == GLOBAL_BACK_CALLBACK)
async def on_back(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    if cb.message:
        await cb.message.answer("Ок, ничего не менял.")


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_ADD, F.data.startswith("stopadd_page:"))
async def on_add_page(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    ack = await safe_callback_ack(cb, callback_type="stopadd_page", logger=logger, started_at=started_at)
    if not cb.message:
        return
    try:
        requested_page = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    data = await state.get_data()
    suggestions = [str(item) for item in (data.get("stopword_add_candidates") or []) if str(item).strip()]
    page_before = int(data.get("stopword_add_page") or 0)
    page_after = _clamp_picker_page(requested_page, total=len(suggestions), page_size=_PAGE_SIZE)
    await state.update_data(stopword_add_page=page_after)
    data["stopword_add_page"] = page_after
    await _render_add_picker(
        cb.message,
        state,
        data=data,
        edit_mode="markup",
        callback_info={"ack": ack, "page_before": page_before},
    )


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_ADD, F.data.startswith("stopadd_toggle:"))
async def on_add_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    ack = await safe_callback_ack(cb, callback_type="stopadd_toggle", logger=logger, started_at=started_at)
    if not cb.message:
        return
    try:
        idx = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    data = await state.get_data()
    page_before = int(data.get("stopword_add_page") or 0)
    selected = {int(item) for item in (data.get("stopword_add_selected_ids") or [])}
    if idx in selected:
        selected.remove(idx)
    else:
        selected.add(idx)
    selected_sorted = sorted(selected)
    await state.update_data(stopword_add_selected_ids=selected_sorted)
    data["stopword_add_selected_ids"] = selected_sorted
    await _render_add_picker(
        cb.message,
        state,
        data=data,
        edit_mode="markup",
        callback_info={"ack": ack, "page_before": page_before},
    )


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_ADD, F.data == "stopadd_all")
async def on_add_all(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    ack = await safe_callback_ack(cb, callback_type="stopadd_all", logger=logger, started_at=started_at)
    if not cb.message:
        return
    data = await state.get_data()
    page_before = int(data.get("stopword_add_page") or 0)
    suggestions = list(data.get("stopword_add_candidates") or [])
    selected_ids = list(range(len(suggestions)))
    await state.update_data(stopword_add_selected_ids=selected_ids)
    data["stopword_add_selected_ids"] = selected_ids
    await _render_add_picker(
        cb.message,
        state,
        data=data,
        edit_mode="markup",
        callback_info={"ack": ack, "page_before": page_before},
    )


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_ADD, F.data == "stopadd_done")
async def on_add_done(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    if not cb.message:
        return
    data = await state.get_data()
    page_before = int(data.get("stopword_add_page") or 0)
    selected = {int(item) for item in (data.get("stopword_add_selected_ids") or [])}
    suggestions = [str(item) for item in (data.get("stopword_add_candidates") or []) if str(item).strip()]
    if not selected:
        ack = await safe_callback_ack(
            cb,
            callback_type="stopadd_done",
            logger=logger,
            started_at=started_at,
            text="Сначала выбери хотя бы одну фразу.",
            show_alert=True,
        )
        _log_picker_callback(
            ack=ack,
            page_before=page_before,
            page_after=page_before,
            candidate_count=len(suggestions),
            keyboard_render_ms=0.0,
            status="empty_selection",
        )
        return
    ack = await safe_callback_ack(cb, callback_type="stopadd_done", logger=logger, started_at=started_at)

    user = await db_call(TgUser.objects.get, id=data["user_id"])
    current = await db_run(lambda: get_user_report_stopwords(user=user))
    current_set = set(current)
    chosen = [suggestions[idx] for idx in sorted(selected) if 0 <= idx < len(suggestions)]
    added_words = [word for word in chosen if word not in current_set]
    next_stopwords = current + added_words
    await db_run(lambda: _save_user_stopwords(user=user, stopwords=next_stopwords))
    await state.clear()
    await cb.message.answer(
        _summary_lines(
            action="Добавлено",
            changed=len(added_words),
            skipped=len(chosen) - len(added_words),
            total=len(next_stopwords),
        )
    )
    _log_picker_callback(
        ack=ack,
        page_before=page_before,
        page_after=page_before,
        candidate_count=len(suggestions),
        keyboard_render_ms=0.0,
        status="success",
    )


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_REMOVE, F.data.startswith("stoprem_page:"))
async def on_remove_page(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    ack = await safe_callback_ack(cb, callback_type="stoprem_page", logger=logger, started_at=started_at)
    if not cb.message:
        return
    try:
        requested_page = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    data = await state.get_data()
    stopword_items = [str(item) for item in (data.get("stopword_remove_items") or []) if str(item).strip()]
    page_before = int(data.get("stopword_remove_page") or 0)
    page_after = _clamp_picker_page(requested_page, total=len(stopword_items), page_size=_PAGE_SIZE)
    await state.update_data(stopword_remove_page=page_after)
    data["stopword_remove_page"] = page_after
    await _render_remove_picker(
        cb.message,
        state,
        data=data,
        edit_mode="markup",
        callback_info={"ack": ack, "page_before": page_before},
    )


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_REMOVE, F.data.startswith("stoprem_toggle:"))
async def on_remove_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    ack = await safe_callback_ack(cb, callback_type="stoprem_toggle", logger=logger, started_at=started_at)
    if not cb.message:
        return
    try:
        idx = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    data = await state.get_data()
    page_before = int(data.get("stopword_remove_page") or 0)
    selected = {int(item) for item in (data.get("stopword_remove_selected_ids") or [])}
    if idx in selected:
        selected.remove(idx)
    else:
        selected.add(idx)
    selected_sorted = sorted(selected)
    await state.update_data(stopword_remove_selected_ids=selected_sorted)
    data["stopword_remove_selected_ids"] = selected_sorted
    await _render_remove_picker(
        cb.message,
        state,
        data=data,
        edit_mode="markup",
        callback_info={"ack": ack, "page_before": page_before},
    )


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_REMOVE, F.data == "stoprem_all")
async def on_remove_all(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    ack = await safe_callback_ack(cb, callback_type="stoprem_all", logger=logger, started_at=started_at)
    if not cb.message:
        return
    data = await state.get_data()
    page_before = int(data.get("stopword_remove_page") or 0)
    current = list(data.get("stopword_remove_items") or [])
    selected_ids = list(range(len(current)))
    await state.update_data(stopword_remove_selected_ids=selected_ids)
    data["stopword_remove_selected_ids"] = selected_ids
    await _render_remove_picker(
        cb.message,
        state,
        data=data,
        edit_mode="markup",
        callback_info={"ack": ack, "page_before": page_before},
    )


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_REMOVE, F.data == "stoprem_done")
async def on_remove_done(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    if not cb.message:
        return
    data = await state.get_data()
    page_before = int(data.get("stopword_remove_page") or 0)
    selected = {int(item) for item in (data.get("stopword_remove_selected_ids") or [])}
    current = [str(item) for item in (data.get("stopword_remove_items") or []) if str(item).strip()]
    if not selected:
        ack = await safe_callback_ack(
            cb,
            callback_type="stoprem_done",
            logger=logger,
            started_at=started_at,
            text="Сначала выбери хотя бы одну фразу.",
            show_alert=True,
        )
        _log_picker_callback(
            ack=ack,
            page_before=page_before,
            page_after=page_before,
            candidate_count=len(current),
            keyboard_render_ms=0.0,
            status="empty_selection",
        )
        return
    ack = await safe_callback_ack(cb, callback_type="stoprem_done", logger=logger, started_at=started_at)

    user = await db_call(TgUser.objects.get, id=data["user_id"])
    to_remove = {current[idx] for idx in selected if 0 <= idx < len(current)}
    next_stopwords = [word for word in current if word not in to_remove]
    removed = len(current) - len(next_stopwords)
    await db_run(lambda: _save_user_stopwords(user=user, stopwords=next_stopwords))
    await state.clear()
    await cb.message.answer(
        _summary_lines(
            action="Удалено",
            changed=removed,
            skipped=max(0, len(selected) - removed),
            total=len(next_stopwords),
        )
    )
    _log_picker_callback(
        ack=ack,
        page_before=page_before,
        page_after=page_before,
        candidate_count=len(current),
        keyboard_render_ms=0.0,
        status="success",
    )


@router.message(StopwordManagementStates.PICK_STOPWORDS_ADD)
@router.message(StopwordManagementStates.PICK_STOPWORDS_REMOVE)
async def on_picker_text(message: Message) -> None:
    await message.answer("Выбери stopwords кнопками ниже.")
