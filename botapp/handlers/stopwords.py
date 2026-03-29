from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from botapp.db import db_call, db_run
from botapp.keyboards import GLOBAL_BACK_CALLBACK, kb_manage_stopwords
from botapp.state import StopwordManagementStates
from botapp.user_sync import upsert_tg_user
from tracking.models import TgUser
from tracking.services.report_filters import get_user_report_stopwords
from tracking.services.stopword_suggestions import build_user_stopword_suggestions

router = Router()

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


def _picker_text(*, title: str, selected_total: int, total: int) -> str:
    return "\n".join(
        [
            title,
            f"Выбрано: {selected_total}/{total}",
            "Когда готово, нажми «Готово».",
        ]
    )


async def _render_add_picker(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    suggestions = [str(item) for item in (data.get("stopword_add_candidates") or []) if str(item).strip()]
    selected = {int(item) for item in (data.get("stopword_add_selected_ids") or [])}
    page = int(data.get("stopword_add_page") or 0)
    text = _picker_text(
        title="Выбери фразы, которые нужно добавить в stopwords.",
        selected_total=len(selected),
        total=len(suggestions),
    )
    kb = kb_manage_stopwords(
        stopwords=suggestions,
        selected_ids=selected,
        page=page,
        page_size=_PAGE_SIZE,
        toggle_prefix="stopadd_toggle",
        page_prefix="stopadd_page",
        all_callback="stopadd_all",
        done_callback="stopadd_done",
        done_text="Добавить",
    )
    try:
        await message.edit_text(text, reply_markup=kb)
    except Exception:
        try:
            await message.edit_reply_markup(reply_markup=kb)
        except Exception:
            return


async def _render_remove_picker(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    stopwords = [str(item) for item in (data.get("stopword_remove_items") or []) if str(item).strip()]
    selected = {int(item) for item in (data.get("stopword_remove_selected_ids") or [])}
    page = int(data.get("stopword_remove_page") or 0)
    text = _picker_text(
        title="Выбери stopwords, которые нужно удалить.",
        selected_total=len(selected),
        total=len(stopwords),
    )
    kb = kb_manage_stopwords(
        stopwords=stopwords,
        selected_ids=selected,
        page=page,
        page_size=_PAGE_SIZE,
        toggle_prefix="stoprem_toggle",
        page_prefix="stoprem_page",
        all_callback="stoprem_all",
        done_callback="stoprem_done",
        done_text="Удалить",
    )
    try:
        await message.edit_text(text, reply_markup=kb)
    except Exception:
        try:
            await message.edit_reply_markup(reply_markup=kb)
        except Exception:
            return


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
    await cb.answer()
    if not cb.message:
        return
    try:
        page = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    await state.update_data(stopword_add_page=max(0, page))
    await _render_add_picker(cb.message, state)


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_ADD, F.data.startswith("stopadd_toggle:"))
async def on_add_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    try:
        idx = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    data = await state.get_data()
    selected = {int(item) for item in (data.get("stopword_add_selected_ids") or [])}
    if idx in selected:
        selected.remove(idx)
    else:
        selected.add(idx)
    await state.update_data(stopword_add_selected_ids=sorted(selected))
    await _render_add_picker(cb.message, state)


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_ADD, F.data == "stopadd_all")
async def on_add_all(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    data = await state.get_data()
    suggestions = list(data.get("stopword_add_candidates") or [])
    await state.update_data(stopword_add_selected_ids=list(range(len(suggestions))))
    await _render_add_picker(cb.message, state)


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_ADD, F.data == "stopadd_done")
async def on_add_done(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    data = await state.get_data()
    selected = {int(item) for item in (data.get("stopword_add_selected_ids") or [])}
    suggestions = [str(item) for item in (data.get("stopword_add_candidates") or []) if str(item).strip()]
    if not selected:
        await cb.answer("Сначала выбери хотя бы одну фразу.", show_alert=True)
        return

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


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_REMOVE, F.data.startswith("stoprem_page:"))
async def on_remove_page(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    try:
        page = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    await state.update_data(stopword_remove_page=max(0, page))
    await _render_remove_picker(cb.message, state)


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_REMOVE, F.data.startswith("stoprem_toggle:"))
async def on_remove_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    try:
        idx = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    data = await state.get_data()
    selected = {int(item) for item in (data.get("stopword_remove_selected_ids") or [])}
    if idx in selected:
        selected.remove(idx)
    else:
        selected.add(idx)
    await state.update_data(stopword_remove_selected_ids=sorted(selected))
    await _render_remove_picker(cb.message, state)


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_REMOVE, F.data == "stoprem_all")
async def on_remove_all(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    data = await state.get_data()
    current = list(data.get("stopword_remove_items") or [])
    await state.update_data(stopword_remove_selected_ids=list(range(len(current))))
    await _render_remove_picker(cb.message, state)


@router.callback_query(StopwordManagementStates.PICK_STOPWORDS_REMOVE, F.data == "stoprem_done")
async def on_remove_done(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    data = await state.get_data()
    selected = {int(item) for item in (data.get("stopword_remove_selected_ids") or [])}
    current = [str(item) for item in (data.get("stopword_remove_items") or []) if str(item).strip()]
    if not selected:
        await cb.answer("Сначала выбери хотя бы одну фразу.", show_alert=True)
        return

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


@router.message(StopwordManagementStates.PICK_STOPWORDS_ADD)
@router.message(StopwordManagementStates.PICK_STOPWORDS_REMOVE)
async def on_picker_text(message: Message) -> None:
    await message.answer("Выбери stopwords кнопками ниже.")
