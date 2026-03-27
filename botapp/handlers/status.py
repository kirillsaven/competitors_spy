from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from botapp.db import db_call, db_run
from botapp.user_sync import upsert_tg_user
from common.time import format_dt_local, format_timezone_label
from tracking.models import Platform, Schedule, UserCompetitor
from tracking.tasks import run_user_report_now

router = Router()


def _platform_counts(*, user: TgUser) -> dict[str, int]:
    return {
        platform: UserCompetitor.objects.filter(user=user, is_active=True, competitor__platform=platform).count()
        for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM)
    }


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
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

    counts = await db_run(lambda: _platform_counts(user=user))
    schedule = await db_run(lambda: Schedule.objects.filter(user=user).first())
    if not schedule or not schedule.is_enabled or not schedule.next_run_at:
        await message.answer(
            "Конкуренты:\n"
            f"YouTube: {counts.get(Platform.YOUTUBE, 0)}\n"
            f"TikTok: {counts.get(Platform.TIKTOK, 0)}\n"
            f"Instagram: {counts.get(Platform.INSTAGRAM, 0)}\n"
            "Расписание: не настроено. Запусти /setup."
        )
        return

    await message.answer(
        "Конкуренты:\n"
        f"YouTube: {counts.get(Platform.YOUTUBE, 0)}\n"
        f"TikTok: {counts.get(Platform.TIKTOK, 0)}\n"
        f"Instagram: {counts.get(Platform.INSTAGRAM, 0)}\n"
        f"Таймзона: {format_timezone_label(user.timezone_str)}\n"
        f"Время отчетов: {', '.join(schedule.times or [])}\n"
        f"Следующий отчет: {format_dt_local(schedule.next_run_at, user.timezone_str)}"
    )


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
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
    schedule = await db_run(lambda: Schedule.objects.filter(user=user).first())
    if not schedule or not schedule.is_enabled:
        await message.answer("Сначала запусти /setup.")
        return
    if schedule and schedule.is_running:
        await message.answer("Отчет уже собирается. Пришлю сообщением, когда будет готов.")
        return
    run_user_report_now.delay(user.id)
    await message.answer("Собираю отчет. Пришлю сообщением, когда будет готов.")


@router.message(Command("competitors"))
async def cmd_competitors(message: Message) -> None:
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
    links = await db_run(
        lambda: list(
            UserCompetitor.objects.select_related("competitor")
            .filter(user=user, is_active=True)
            .order_by("id")
        )
    )
    comps = [lnk.competitor for lnk in links]
    if not comps:
        await message.answer("Конкуренты не настроены. Запусти /setup.")
        return
    lines = ["Конкуренты:"]
    for i, c in enumerate(comps, start=1):
        name = c.display_name or c.handle or c.external_id
        platform_label = {
            Platform.YOUTUBE: "YouTube",
            Platform.TIKTOK: "TikTok",
            Platform.INSTAGRAM: "Instagram",
        }.get(c.platform, c.platform)
        lines.append(f"{i}) [{platform_label}] {name}")
    lines.append("")
    lines.append("Изменить список можно через /setup.")
    await message.answer("\n".join(lines))
