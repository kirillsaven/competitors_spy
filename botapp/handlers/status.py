from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from django.utils import timezone

from botapp.db import db_run
from common.time import format_dt_local
from tracking.models import Platform, Schedule, TgUser
from tracking.tasks import run_user_report

router = Router()


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if not message.from_user:
        return
    user = await db_run(lambda: TgUser.objects.filter(tg_user_id=message.from_user.id).first())
    if not user:
        await message.answer("Сначала запусти /setup.")
        return

    comps = await db_run(lambda: user.competitors.filter(platform=Platform.YOUTUBE, is_active=True).count())
    schedule = await db_run(lambda: Schedule.objects.filter(user=user).first())
    if not schedule or not schedule.is_enabled or not schedule.next_run_at:
        await message.answer(f"Конкуренты (YouTube): {comps}\nРасписание: не настроено. Запусти /setup.")
        return

    await message.answer(
        f"Конкуренты (YouTube): {comps}\n"
        f"Таймзона: {user.timezone_str}\n"
        f"Время отчетов: {', '.join(schedule.times or [])}\n"
        f"Следующий отчет: {format_dt_local(schedule.next_run_at, user.timezone_str)}"
    )


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    if not message.from_user:
        return
    user = await db_run(lambda: TgUser.objects.filter(tg_user_id=message.from_user.id).first())
    if not user:
        await message.answer("Сначала запусти /setup.")
        return
    run_user_report.delay(user.id)
    await message.answer("Ок, собираю отчет. Пришлю сообщением, когда будет готов.")


@router.message(Command("competitors"))
async def cmd_competitors(message: Message) -> None:
    if not message.from_user:
        return
    user = await db_run(lambda: TgUser.objects.filter(tg_user_id=message.from_user.id).first())
    if not user:
        await message.answer("Сначала запусти /setup.")
        return
    comps = await db_run(lambda: list(user.competitors.filter(platform=Platform.YOUTUBE, is_active=True).order_by("id")))
    if not comps:
        await message.answer("Конкуренты не настроены. Запусти /setup.")
        return
    lines = ["Конкуренты (YouTube):"]
    for i, c in enumerate(comps, start=1):
        name = c.display_name or c.handle or c.external_id
        lines.append(f"{i}) {name}")
    lines.append("")
    lines.append("Изменить список можно через /setup.")
    await message.answer("\n".join(lines))
