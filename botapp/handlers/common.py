from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from botapp.db import db_call
from botapp.user_sync import upsert_tg_user

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not message.from_user:
        return
    await db_call(
        upsert_tg_user,
        telegram_user_id=message.from_user.id,
        chat_id=message.chat.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        language_code=message.from_user.language_code,
    )
    await message.answer(
        "Привет! Я бот для отслеживания контента конкурентов.\n\n"
        "Команды:\n"
        "/setup - настройка\n"
        "/status - статус\n"
        "/competitors - список конкурентов\n"
        "/competitors_add - добавить конкурентов\n"
        "/competitors_remove - убрать конкурентов\n"
        "/schedule - изменить расписание\n"
        "/report - отчет сейчас\n"
        "/help - помощь\n"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Сейчас отчеты поддерживают YouTube, TikTok и Instagram.\n"
        "Автоподбор конкурентов пока есть только для YouTube, а TikTok/Instagram добавляются вручную ссылками или хендлами.\n\n"
        "/setup - настроить нишу, конкурентов и расписание\n"
        "/status - показать конкурентов и следующее время отчета\n"
        "/competitors - показать активный список\n"
        "/competitors_add - добавить конкурентов вручную\n"
        "/competitors_remove - убрать конкурентов из активного списка\n"
        "/report - запросить отчет прямо сейчас\n"
    )
