from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from botapp.db import db_call
from tracking.models import TgUser

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not message.from_user:
        return
    await db_call(
        TgUser.objects.update_or_create,
        tg_user_id=message.from_user.id,
        defaults={"tg_chat_id": message.chat.id},
    )
    await message.answer(
        "Привет! Я бот для отслеживания контента конкурентов.\n\n"
        "Команды:\n"
        "/setup - настройка\n"
        "/status - статус\n"
        "/competitors - список конкурентов\n"
        "/schedule - изменить расписание\n"
        "/report - отчет сейчас\n"
        "/help - помощь\n"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "MVP поддерживает только YouTube.\n\n"
        "/setup - настроить нишу, конкурентов и расписание\n"
        "/status - показать конкурентов и следующее время отчета\n"
        "/report - запросить отчет прямо сейчас\n"
    )
