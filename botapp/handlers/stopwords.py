from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from botapp.db import db_call, db_run
from botapp.state import StopwordManagementStates
from botapp.user_sync import upsert_tg_user
from tracking.models import TgUser
from tracking.services.report_filters import get_user_report_stopwords, parse_stopwords_input

router = Router()


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
            "/stopwords_add - добавить\n"
            "/stopwords_remove - удалить"
        )
        return
    lines = ["Стоп-слова отчета:"]
    for index, word in enumerate(stopwords, start=1):
        lines.append(f"{index}. {word}")
    lines.append("")
    lines.append("/stopwords_add - добавить")
    lines.append("/stopwords_remove - удалить")
    await message.answer("\n".join(lines))


@router.message(Command("stopwords_add"))
async def cmd_stopwords_add(message: Message, state: FSMContext) -> None:
    await state.set_state(StopwordManagementStates.WAIT_STOPWORDS_ADD_INPUT)
    await message.answer(
        "Пришли слова или фразы по одному в строке или через запятую.\n"
        "Я добавлю их в твои stopwords для отчетов."
    )


@router.message(Command("stopwords_remove"))
async def cmd_stopwords_remove(message: Message, state: FSMContext) -> None:
    await state.set_state(StopwordManagementStates.WAIT_STOPWORDS_REMOVE_INPUT)
    await message.answer(
        "Пришли слова или фразы по одному в строке или через запятую.\n"
        "Я удалю их только из твоих stopwords для отчетов."
    )


@router.message(StopwordManagementStates.WAIT_STOPWORDS_ADD_INPUT)
async def on_stopwords_add_input(message: Message, state: FSMContext) -> None:
    raw_words = parse_stopwords_input(message.text)
    if not raw_words or not message.from_user:
        await message.answer("Не вижу валидных слов. Пришли слова или фразы по одному в строке или через запятую.")
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
    current_set = set(current)
    added_words = [word for word in raw_words if word not in current_set]
    next_stopwords = current + added_words
    await db_run(lambda: _save_user_stopwords(user=user, stopwords=next_stopwords))
    await state.set_state(None)
    await message.answer(
        _summary_lines(
            action="Добавлено",
            changed=len(added_words),
            skipped=len(raw_words) - len(added_words),
            total=len(next_stopwords),
        )
    )


@router.message(StopwordManagementStates.WAIT_STOPWORDS_REMOVE_INPUT)
async def on_stopwords_remove_input(message: Message, state: FSMContext) -> None:
    raw_words = parse_stopwords_input(message.text)
    if not raw_words or not message.from_user:
        await message.answer("Не вижу валидных слов. Пришли слова или фразы по одному в строке или через запятую.")
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
    raw_set = set(raw_words)
    next_stopwords = [word for word in current if word not in raw_set]
    removed = len(current) - len(next_stopwords)
    await db_run(lambda: _save_user_stopwords(user=user, stopwords=next_stopwords))
    await state.set_state(None)
    await message.answer(
        _summary_lines(
            action="Удалено",
            changed=removed,
            skipped=len(raw_words) - removed,
            total=len(next_stopwords),
        )
    )
