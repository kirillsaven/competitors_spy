from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from django.conf import settings
from django.utils import timezone

from botapp.db import db_call, db_run
from botapp.keyboards import kb_keywords_confirm, kb_reports_per_day, kb_timezone_method
from botapp.state import SetupStates
from common.time import TimeParseError, TimezoneParseError, compute_next_run_at, normalize_timezone_str, parse_hhmm
from tracking.models import AddedBy, Competitor, Platform, Schedule, SeedProfile, SeedStatus, TgUser, TzSource
from tracking.services.competitor_service import upsert_competitor
from tracking.services.youtube_service import (
    YouTubeNotConfigured,
    discover_youtube_competitors,
    infer_youtube_keywords,
    resolve_youtube_seed,
)
from tracking.tasks import bootstrap_user_data

logger = logging.getLogger(__name__)
router = Router()


def _parse_keywords(text: str) -> list[str]:
    parts = [p.strip() for p in (text or "").split(",")]
    kws = [p for p in parts if p]
    return kws[:12]


@router.message(Command("setup"))
async def cmd_setup(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    user, _ = await db_call(
        TgUser.objects.update_or_create,
        tg_user_id=message.from_user.id,
        defaults={"tg_chat_id": message.chat.id},
    )
    await state.clear()
    await state.update_data(user_id=user.id)
    await state.set_state(SetupStates.WAIT_SEED_INPUT)
    await message.answer(
        "Ок, давай настроим.\n\n"
        "Пришли ссылку/хендл профиля (можно любой платформы) или просто коротко опиши нишу текстом."
    )


@router.message(Command("schedule"))
async def cmd_schedule(message: Message, state: FSMContext) -> None:
    """
    Update schedule only (keep competitors as-is).
    """
    if not message.from_user:
        return
    user, _ = await db_call(
        TgUser.objects.update_or_create,
        tg_user_id=message.from_user.id,
        defaults={"tg_chat_id": message.chat.id},
    )
    await state.clear()
    await state.update_data(user_id=user.id)
    await _ask_reports_per_day(message, state)


@router.message(SetupStates.WAIT_SEED_INPUT)
async def on_seed_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пришли ссылку/хендл или текст нишы.")
        return

    seed = None
    niche_keywords: list[str] = []
    niche_source = "manual"
    detected_platform = ""
    canonical_url = ""
    try:
        seed = await asyncio.to_thread(resolve_youtube_seed, raw)
    except YouTubeNotConfigured:
        seed = None
    except Exception as e:
        logger.warning("Seed resolve failed: %s", e)
        seed = None

    if seed:
        detected_platform = "youtube"
        canonical_url = seed.url
        try:
            niche_keywords = await asyncio.to_thread(infer_youtube_keywords, seed)
            niche_source = "auto"
        except Exception as e:
            logger.warning("Keyword inference failed: %s", e)
            niche_keywords = []

    sp = await db_call(
        SeedProfile.objects.create,
        user=user,
        raw_input=raw,
        detected_platform=detected_platform,
        canonical_url=canonical_url,
        niche_keywords=niche_keywords,
        niche_source=niche_source,
        status=SeedStatus.RESOLVED if seed else SeedStatus.PENDING,
    )

    await state.update_data(seed_profile_id=sp.id, seed=seed)

    if niche_keywords:
        await state.set_state(SetupStates.CONFIRM_OR_EDIT_NICHE)
        await message.answer(
            "Я определил ключевые слова:\n" + ", ".join(niche_keywords) + "\n\nПодтвердить?",
            reply_markup=kb_keywords_confirm(),
        )
    else:
        await state.set_state(SetupStates.WAIT_MANUAL_NICHE)
        await message.answer("Введи ключевые слова для ниши через запятую (например: маркетинг, продажи, b2b).")


@router.callback_query(SetupStates.CONFIRM_OR_EDIT_NICHE, F.data.in_(["kw_ok", "kw_edit"]))
async def on_keywords_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if cb.data == "kw_ok":
        await state.set_state(SetupStates.WAIT_COMPETITOR_LIST)
        await cb.message.answer(
            "Пришли список конкурентов (YouTube ссылки/хендлы), по одному в строке.\n"
            "Если пропустить: отправь /skip"
        )
    else:
        await state.set_state(SetupStates.WAIT_MANUAL_NICHE)
        await cb.message.answer("Ок. Введи ключевые слова через запятую.")


@router.message(Command("skip"), SetupStates.WAIT_COMPETITOR_LIST)
async def on_skip_competitors(message: Message, state: FSMContext) -> None:
    await _start_discovery(message, state)


@router.message(SetupStates.WAIT_MANUAL_NICHE)
async def on_manual_niche(message: Message, state: FSMContext) -> None:
    kws = _parse_keywords(message.text or "")
    if not kws:
        await message.answer("Не вижу ключевых слов. Введи через запятую.")
        return

    data = await state.get_data()
    sp_id = data.get("seed_profile_id")
    if sp_id:
        await db_run(
            lambda: SeedProfile.objects.filter(id=sp_id).update(
                niche_keywords=kws, niche_source="manual", status=SeedStatus.RESOLVED
            )
        )

    await state.update_data(niche_keywords=kws)
    await state.set_state(SetupStates.WAIT_COMPETITOR_LIST)
    await message.answer(
        "Принято.\n\nПришли список конкурентов (YouTube ссылки/хендлы), по одному в строке.\n"
        "Если пропустить: отправь /skip"
    )


@router.message(SetupStates.WAIT_COMPETITOR_LIST)
async def on_competitor_list(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пришли список или /skip.")
        return

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    limit = int(getattr(settings, "MAX_COMPETITORS_YOUTUBE", 20))
    current = await db_run(lambda: user.competitors.filter(platform=Platform.YOUTUBE, is_active=True).count())
    remaining = max(0, limit - current)
    if remaining <= 0:
        await message.answer(f"Лимит конкурентов достигнут ({limit}).")
        await _start_discovery(message, state)
        return

    added = 0
    for ln in lines[: min(50, remaining)]:
        try:
            seed = await asyncio.to_thread(resolve_youtube_seed, ln)
        except Exception:
            seed = None
        if not seed:
            continue
        await db_call(
            upsert_competitor,
            user=user,
            platform=Platform.YOUTUBE,
            external_id=seed.external_id,
            handle=seed.handle,
            url=seed.url,
            display_name=seed.title,
            added_by=AddedBy.MANUAL,
            meta={"uploads_playlist_id": seed.uploads_playlist_id} if seed.uploads_playlist_id else None,
        )
        added += 1

    await message.answer(f"Добавил конкурентов: {added}.")
    await _start_discovery(message, state)


async def _start_discovery(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    sp_id = data.get("seed_profile_id")
    sp = await db_call(SeedProfile.objects.get, id=sp_id) if sp_id else None
    kws = (sp.niche_keywords if sp else []) or data.get("niche_keywords") or []
    seed = data.get("seed")

    await message.answer("Ищу кандидатов-конкурентов на YouTube...")
    try:
        candidates = await asyncio.to_thread(
            discover_youtube_competitors,
            keywords=list(kws),
            seed=seed,
            max_search_calls=int(getattr(settings, "YT_MAX_SEARCH_CALLS_PER_SETUP", 3)),
            max_candidates=20,
        )
    except Exception as e:
        logger.warning("Discovery failed: %s", e)
        candidates = []

    # Filter out existing competitors.
    existing = await db_run(
        lambda: set(
            user.competitors.filter(platform=Platform.YOUTUBE, is_active=True).values_list("external_id", flat=True)
        )
    )
    candidates = [c for c in candidates if c.external_id not in existing]

    await state.update_data(candidates=[c.__dict__ for c in candidates])
    await state.set_state(SetupStates.SHOW_AUTO_CANDIDATES)

    if not candidates:
        await message.answer("Кандидатов не нашел. Переходим к расписанию.")
        await _ask_reports_per_day(message, state)
        return

    lines = ["Кандидаты (ответь номерами через пробел, например: 1 3 5; или 0 чтобы пропустить):"]
    for i, c in enumerate(candidates, start=1):
        name = c.display_name or c.handle or c.external_id
        lines.append(f"{i}) {name} ({c.reason})")
    await message.answer("\n".join(lines))


@router.message(SetupStates.SHOW_AUTO_CANDIDATES)
async def on_pick_candidates(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    candidates = data.get("candidates") or []
    txt = (message.text or "").strip()
    if txt == "0":
        await message.answer("Ок, пропускаю.")
        await _review_competitors(message, state)
        return

    nums = re.findall(r"\d+", txt)
    picks = {int(n) for n in nums if n.isdigit()}
    limit = int(getattr(settings, "MAX_COMPETITORS_YOUTUBE", 20))
    current = await db_run(lambda: user.competitors.filter(platform=Platform.YOUTUBE, is_active=True).count())
    added = 0
    for i, c in enumerate(candidates, start=1):
        if i not in picks:
            continue
        if current + added >= limit:
            break
        await db_call(
            upsert_competitor,
            user=user,
            platform=Platform.YOUTUBE,
            external_id=c["external_id"],
            handle=c.get("handle"),
            url=c.get("url") or "",
            display_name=c.get("display_name"),
            added_by=AddedBy.AUTO,
            meta=None,
        )
        added += 1

    msg = f"Добавил кандидатов: {added}."
    if current + added >= limit:
        msg += f" Достигнут лимит конкурентов ({limit})."
    await message.answer(msg)
    await _review_competitors(message, state)


async def _review_competitors(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    comps = await db_run(lambda: list(user.competitors.filter(platform=Platform.YOUTUBE, is_active=True).order_by("id")))
    if not comps:
        await message.answer("Список конкурентов пуст. Можно вернуться и добавить конкурентов вручную через /setup.")
        await _ask_reports_per_day(message, state)
        return

    await state.set_state(SetupStates.REVIEW_FINAL_COMPETITORS)
    lines = ["Финальный список конкурентов. Ответь 'ok' или пришли номера для удаления:"]
    for i, c in enumerate(comps, start=1):
        name = c.display_name or c.handle or c.external_id
        lines.append(f"{i}) {name}")
    await state.update_data(review_competitor_ids=[c.id for c in comps])
    await message.answer("\n".join(lines))


@router.message(SetupStates.REVIEW_FINAL_COMPETITORS)
async def on_review_competitors(message: Message, state: FSMContext) -> None:
    txt = (message.text or "").strip().lower()
    if txt in {"ok", "готово", "done"}:
        await _ask_reports_per_day(message, state)
        return

    nums = re.findall(r"\d+", txt)
    if not nums:
        await message.answer("Не понял. Ответь 'ok' или пришли номера (например: 2 4).")
        return

    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    ids = data.get("review_competitor_ids") or []
    picks = sorted({int(n) for n in nums if n.isdigit()})
    to_remove = []
    for p in picks:
        if 1 <= p <= len(ids):
            to_remove.append(ids[p - 1])
    if to_remove:
        await db_run(lambda: user.competitors.filter(id__in=to_remove).update(is_active=False))
        await message.answer(f"Удалил: {len(to_remove)}.")
    await _review_competitors(message, state)


async def _ask_reports_per_day(message: Message, state: FSMContext) -> None:
    await state.set_state(SetupStates.ASK_REPORTS_PER_DAY)
    await message.answer("Сколько раз в день присылать отчет?", reply_markup=kb_reports_per_day())


@router.callback_query(SetupStates.ASK_REPORTS_PER_DAY, F.data.in_(["rpd_1", "rpd_2"]))
async def on_reports_per_day(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    rpd = 1 if cb.data == "rpd_1" else 2
    await state.update_data(reports_per_day=rpd, times=[])
    await state.set_state(SetupStates.WAIT_TIME_1)
    await cb.message.answer("Введи время первого отчета в формате HH:MM (например, 09:00).")


@router.message(SetupStates.WAIT_TIME_1)
async def on_time_1(message: Message, state: FSMContext) -> None:
    try:
        t = parse_hhmm(message.text or "")
    except TimeParseError as e:
        await message.answer(str(e))
        return
    data = await state.get_data()
    times = list(data.get("times") or [])
    times.append(f"{t.hour:02d}:{t.minute:02d}")
    await state.update_data(times=times)
    if int(data.get("reports_per_day") or 1) == 2:
        await state.set_state(SetupStates.WAIT_TIME_2)
        await message.answer("Введи время второго отчета в формате HH:MM (например, 21:00).")
    else:
        await _ask_timezone_method(message, state)


@router.message(SetupStates.WAIT_TIME_2)
async def on_time_2(message: Message, state: FSMContext) -> None:
    try:
        t = parse_hhmm(message.text or "")
    except TimeParseError as e:
        await message.answer(str(e))
        return
    data = await state.get_data()
    times = list(data.get("times") or [])
    times.append(f"{t.hour:02d}:{t.minute:02d}")
    await state.update_data(times=times)
    await _ask_timezone_method(message, state)


async def _ask_timezone_method(message: Message, state: FSMContext) -> None:
    await state.set_state(SetupStates.ASK_TIMEZONE_METHOD)
    await message.answer(
        "Как зададим таймзону? Можно отправить геолокацию (опционально), или ввести вручную.\n"
        f"По умолчанию: {getattr(settings, 'DEFAULT_TIMEZONE', 'Europe/Moscow')}",
        reply_markup=kb_timezone_method(),
    )


@router.message(SetupStates.ASK_TIMEZONE_METHOD, F.location)
async def on_timezone_location(message: Message, state: FSMContext) -> None:
    # Some clients send location immediately after pressing the button.
    await state.set_state(SetupStates.WAIT_LOCATION)
    await on_location(message, state)


@router.message(SetupStates.ASK_TIMEZONE_METHOD)
async def on_timezone_method_text(message: Message, state: FSMContext) -> None:
    txt = (message.text or "").strip().lower()
    if "вруч" in txt:
        await state.set_state(SetupStates.WAIT_TZ_MANUAL)
        await message.answer("Введи таймзону: например `Europe/Moscow` или `UTC+03:00`.", reply_markup=None)
        return
    if "умолч" in txt:
        await _finalize_schedule(message, state, timezone_str=getattr(settings, "DEFAULT_TIMEZONE", "Europe/Moscow"), tz_source=TzSource.DEFAULT)
        return
    await message.answer("Выбери один из вариантов на клавиатуре.")


@router.message(SetupStates.WAIT_LOCATION, F.location)
async def on_location(message: Message, state: FSMContext) -> None:
    lat = message.location.latitude
    lon = message.location.longitude
    tz_name = None
    try:
        from timezonefinder import TimezoneFinder  # optional on Windows host; required in Docker

        tf = TimezoneFinder()
        tz_name = tf.timezone_at(lat=lat, lng=lon)
    except Exception as e:
        logger.warning("timezonefinder failed: %s", e)
        tz_name = None

    if not tz_name:
        await state.set_state(SetupStates.WAIT_TZ_MANUAL)
        await message.answer("Не смог определить таймзону по геолокации. Введи вручную: Europe/Moscow или UTC+03:00.")
        return

    await _finalize_schedule(message, state, timezone_str=tz_name, tz_source=TzSource.LOCATION)


@router.message(SetupStates.WAIT_TZ_MANUAL)
async def on_tz_manual(message: Message, state: FSMContext) -> None:
    raw = message.text or ""
    try:
        tz_str = normalize_timezone_str(raw)
    except TimezoneParseError as e:
        await message.answer(str(e))
        return
    await _finalize_schedule(message, state, timezone_str=tz_str, tz_source=TzSource.MANUAL)


async def _finalize_schedule(message: Message, state: FSMContext, *, timezone_str: str, tz_source: str) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    times = list(data.get("times") or [])
    now = timezone.now()

    user.timezone_str = timezone_str
    user.tz_source = tz_source
    await db_run(lambda: user.save(update_fields=["timezone_str", "tz_source", "updated_at"]))

    next_run_at = compute_next_run_at(user.timezone_str, times, now)
    await db_call(
        Schedule.objects.update_or_create,
        user=user,
        defaults={
            "is_enabled": True,
            "times": times,
            "next_run_at": next_run_at,
        },
    )

    # Initial snapshot to enable delta-based scoring on the next run.
    bootstrap_user_data.delay(user.id)

    await state.clear()
    comp_count = await db_run(lambda: user.competitors.filter(platform=Platform.YOUTUBE, is_active=True).count())
    await message.answer(
        "Готово.\n\n"
        f"Конкуренты (YouTube): {comp_count}\n"
        f"Расписание: {', '.join(times)} ({user.timezone_str})\n"
        f"Следующий отчет: {next_run_at.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        "Сейчас сделаю первичный сбор данных. Первый «полноценный» отчет появится после следующего сбора.\n"
        "Команда /report отправит отчет прямо сейчас.",
        reply_markup=None,
    )
