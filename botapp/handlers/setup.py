from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from django.conf import settings
from django.utils import timezone

from botapp.db import db_call, db_run
from botapp.keyboards import (
    kb_competitors_optional,
    kb_keywords_confirm,
    kb_prune_competitors,
    kb_report_now,
    kb_reports_per_day,
    kb_time_presets_first,
    kb_time_presets_pair,
    kb_time_presets_second,
    kb_time_presets_single,
    kb_timezone_method,
)
from botapp.state import SetupStates
from common.time import (
    TimeParseError,
    TimezoneParseError,
    compute_next_run_at,
    format_dt_local,
    normalize_timezone_str,
    parse_hhmm,
)
from tracking.adapters.base import SeedResolution
from tracking.models import AddedBy, Competitor, Platform, Schedule, SeedProfile, SeedStatus, TgUser, TzSource
from tracking.services.competitor_service import upsert_competitor
from tracking.services.llm_usage import try_consume_llm_call
from tracking.services.niche_service import infer_niche_keywords
from tracking.services.youtube_service import YouTubeNotConfigured, discover_youtube_competitors, resolve_youtube_seed
from tracking.tasks import bootstrap_user_data

logger = logging.getLogger(__name__)
router = Router()


def _seed_to_dict(seed: SeedResolution) -> dict:
    return asdict(seed)


def _seed_from_dict(data: dict) -> SeedResolution:
    return SeedResolution(
        platform=str(data.get("platform") or ""),
        external_id=str(data.get("external_id") or ""),
        handle=data.get("handle"),
        url=str(data.get("url") or ""),
        title=data.get("title"),
        description=data.get("description"),
        uploads_playlist_id=data.get("uploads_playlist_id"),
    )


def _parse_keywords(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"[,;\n]+", text or "") if p.strip()]
    return parts[:12]


def _candidate_display_name(c: dict) -> str:
    return (c.get("display_name") or c.get("handle") or c.get("external_id") or "").strip() or "Без названия"


def _build_prune_text(*, selected: int, total: int, limit: int) -> str:
    lines = [
        "Нашел конкурентов на YouTube.",
        "Нажимай на каналы, чтобы исключить лишних. По умолчанию выбраны все.",
        f"Выбрано: {selected}/{total} (лимит {limit}).",
    ]
    if selected > limit:
        lines.append(f"Нужно исключить еще: {selected - limit}.")
    lines.append("Когда готово, нажми «Готово».")
    return "\n".join(lines)


async def _ask_seed(message: Message, state: FSMContext) -> None:
    await state.set_state(SetupStates.WAIT_SEED_INPUT)
    await message.answer("Пришли ссылку или хендл (handle/nickname) профиля.")


@router.message(Command("setup"))
async def cmd_setup(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return

    user, _ = await db_call(
        TgUser.objects.update_or_create,
        tg_user_id=message.from_user.id,
        defaults={"tg_chat_id": message.chat.id},
    )

    # Reset active competitors for a clean re-setup.
    await db_run(lambda: user.competitors.filter(platform=Platform.YOUTUBE).update(is_active=False))

    await state.clear()
    await state.update_data(user_id=user.id)
    await _ask_seed(message, state)


@router.message(Command("schedule"))
async def cmd_schedule(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    user, _ = await db_call(
        TgUser.objects.update_or_create,
        tg_user_id=message.from_user.id,
        defaults={"tg_chat_id": message.chat.id},
    )
    await state.clear()
    await state.update_data(user_id=user.id)
    await _ask_timezone_method(message, state)


@router.message(SetupStates.WAIT_SEED_INPUT)
async def on_seed_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пришли ссылку или хендл (handle/nickname).")
        return
    if re.search(r"\s", raw):
        await message.answer("Нужна ссылка или хендл (handle/nickname) без пробелов. Например: https://youtube.com/@example или example")
        return

    seed: SeedResolution | None = None
    detected_platform = ""
    canonical_url = ""
    status = SeedStatus.PENDING

    try:
        seed = await asyncio.to_thread(resolve_youtube_seed, raw)
    except YouTubeNotConfigured:
        seed = None
    except Exception as e:
        logger.warning("Seed resolve failed: %s", e)
        seed = None

    if not seed:
        await db_call(
            SeedProfile.objects.create,
            user=user,
            raw_input=raw,
            detected_platform="",
            canonical_url="",
            niche_keywords=[],
            niche_source="manual",
            status=SeedStatus.FAILED,
        )
        await message.answer(
            "Не нашел точного совпадения по этому хендлу (handle/nickname).\n"
            "Пришли ссылку на профиль или хендл еще раз.",
        )
        return

    if seed:
        detected_platform = Platform.YOUTUBE
        canonical_url = seed.url
        status = SeedStatus.RESOLVED

    sp = await db_call(
        SeedProfile.objects.create,
        user=user,
        raw_input=raw,
        detected_platform=detected_platform,
        canonical_url=canonical_url,
        niche_keywords=[],
        niche_source="manual",
        status=status,
    )

    await state.update_data(
        seed_profile_id=sp.id,
        seed=_seed_to_dict(seed) if seed else None,
        competitor_seeds=[],
    )
    await state.set_state(SetupStates.WAIT_COMPETITOR_LIST)
    await message.answer(
        "Если хочешь, пришли конкурентов на YouTube: ссылки или хендлы (handle/nickname), по одному в строке.\n"
        "Это опционально: если ничего не пришлешь, я сам подберу.",
        reply_markup=kb_competitors_optional(),
    )


@router.callback_query(SetupStates.WAIT_COMPETITOR_LIST, F.data.in_(["comp_skip", "comp_done"]))
async def on_competitors_optional(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return

    if cb.data == "comp_skip":
        await state.update_data(competitor_seeds=[])
    await _start_keywords_step(cb.message, state)


@router.message(SetupStates.WAIT_COMPETITOR_LIST)
async def on_competitor_list(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer(
            "Пришли список конкурентов одним сообщением (по одному в строке) или нажми «Дальше».",
            reply_markup=kb_competitors_optional(),
        )
        return

    data = await state.get_data()
    existing = list(data.get("competitor_seeds") or [])
    existing_ids = {str(d.get("external_id") or "") for d in existing}

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    max_manual = 20
    added = 0
    invalid = 0
    skipped = 0

    for ln in lines:
        if len(existing) >= max_manual:
            break
        try:
            s = await asyncio.to_thread(resolve_youtube_seed, ln)
        except Exception:
            s = None
        if not s:
            invalid += 1
            continue
        if s.external_id in existing_ids:
            skipped += 1
            continue
        existing_ids.add(s.external_id)
        existing.append(_seed_to_dict(s))
        added += 1

    await state.update_data(competitor_seeds=existing)

    parts = []
    if added:
        parts.append(f"Запомнил: {added}.")
    if skipped:
        parts.append(f"Повторы: {skipped}.")
    if invalid:
        parts.append(f"Не распознал: {invalid}.")
    if not parts:
        parts.append("Не нашел валидных ссылок.")

    parts.append("Можно прислать еще, или нажми «Дальше».")
    await message.answer(" ".join(parts), reply_markup=kb_competitors_optional())


async def _start_keywords_step(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    sp_id = data.get("seed_profile_id")
    sp = await db_call(SeedProfile.objects.get, id=sp_id) if sp_id else None

    seed_dict = data.get("seed") or None
    seed = _seed_from_dict(seed_dict) if isinstance(seed_dict, dict) else None

    comp_dicts = data.get("competitor_seeds") or []
    comp_seeds: list[SeedResolution] = []
    for d in comp_dicts:
        if isinstance(d, dict):
            try:
                comp_seeds.append(_seed_from_dict(d))
            except Exception:
                continue

    if seed is None and comp_seeds:
        # Fallback: use the first competitor as context seed.
        seed = comp_seeds[0]
        comp_seeds = comp_seeds[1:]

    if seed is None:
        await state.set_state(SetupStates.WAIT_MANUAL_NICHE)
        await message.answer("Чтобы подобрать конкурентов, напиши ключевые слова по нише (через запятую).")
        return

    await message.answer("Секунду, подбираю ключевые слова по нише…")

    prefer_llm = await db_call(try_consume_llm_call, user=user, now_utc=timezone.now())
    try:
        kws, source = await asyncio.to_thread(
            infer_niche_keywords,
            seed=seed,
            competitors=comp_seeds,
            prefer_llm=prefer_llm,
        )
    except Exception as e:
        logger.warning("infer_niche_keywords failed: %s", e)
        kws, source = [], "auto"

    kws = [k.strip() for k in (kws or []) if isinstance(k, str) and k.strip()][:12]
    if not kws:
        await state.set_state(SetupStates.WAIT_MANUAL_NICHE)
        await message.answer("Не получилось надежно определить нишу. Напиши ключевые слова (через запятую).")
        return

    if sp:
        await db_run(
            lambda: SeedProfile.objects.filter(id=sp.id).update(
                niche_keywords=kws,
                niche_source=source,
                status=SeedStatus.RESOLVED,
            )
        )

    await state.update_data(niche_keywords=kws)
    await state.set_state(SetupStates.CONFIRM_OR_EDIT_NICHE)
    await message.answer(
        "Ключевые слова по нише:\n" + "\n".join([f"• {k}" for k in kws]) + "\n\nПодходит?",
        reply_markup=kb_keywords_confirm(),
    )


@router.callback_query(SetupStates.CONFIRM_OR_EDIT_NICHE, F.data.in_(["kw_ok", "kw_edit"]))
async def on_keywords_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    if cb.data == "kw_ok":
        await _start_discovery(cb.message, state)
        return
    await state.set_state(SetupStates.WAIT_MANUAL_NICHE)
    await cb.message.answer("Напиши ключевые слова по нише (через запятую).")


@router.message(SetupStates.WAIT_MANUAL_NICHE)
async def on_manual_niche(message: Message, state: FSMContext) -> None:
    kws = _parse_keywords(message.text or "")
    if not kws:
        await message.answer("Не вижу ключевых слов. Напиши через запятую, например: сборка ПК, комплектующие, ремонт.")
        return

    data = await state.get_data()
    sp_id = data.get("seed_profile_id")
    if sp_id:
        await db_run(
            lambda: SeedProfile.objects.filter(id=sp_id).update(
                niche_keywords=kws,
                niche_source="manual",
                status=SeedStatus.RESOLVED,
            )
        )

    await state.update_data(niche_keywords=kws)
    await _start_discovery(message, state)


async def _start_discovery(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])

    seed_dict = data.get("seed") or None
    seed = _seed_from_dict(seed_dict) if isinstance(seed_dict, dict) else None

    keywords = list(data.get("niche_keywords") or [])

    comp_dicts = data.get("competitor_seeds") or []
    comp_seeds: list[SeedResolution] = []
    for d in comp_dicts:
        if isinstance(d, dict):
            try:
                comp_seeds.append(_seed_from_dict(d))
            except Exception:
                continue

    await message.answer("Подбираю конкурентов на YouTube…")

    auto_candidates = []
    try:
        auto_candidates = await asyncio.to_thread(
            discover_youtube_competitors,
            keywords=keywords,
            seed=seed,
            max_search_calls=int(getattr(settings, "YT_MAX_SEARCH_CALLS_PER_SETUP", 3)),
            max_candidates=20,
            extra_featured_channel_ids=[c.external_id for c in comp_seeds],
        )
    except Exception as e:
        logger.warning("Discovery failed: %s", e)

    # Build combined candidates list (manual + auto), de-duplicated by channelId.
    candidates_by_id: dict[str, dict] = {}

    def add_candidate(d: dict, *, prefer: bool) -> None:
        cid = str(d.get("external_id") or "")
        if not cid:
            return
        if seed and cid == seed.external_id:
            return
        if cid not in candidates_by_id or prefer:
            candidates_by_id[cid] = d

    for s in comp_seeds:
        add_candidate(
            {
                "external_id": s.external_id,
                "handle": s.handle,
                "url": s.url,
                "display_name": s.title,
                "added_by": AddedBy.MANUAL,
                "meta": {"uploads_playlist_id": s.uploads_playlist_id} if s.uploads_playlist_id else {},
                "reason": "manual",
            },
            prefer=True,
        )

    for c in auto_candidates:
        add_candidate(
            {
                "external_id": c.external_id,
                "handle": c.handle,
                "url": c.url,
                "display_name": c.display_name,
                "added_by": AddedBy.AUTO,
                "meta": {},
                "reason": c.reason,
            },
            prefer=False,
        )

    candidates = list(candidates_by_id.values())
    if not candidates:
        await message.answer("Не смог подобрать конкурентов автоматически. Можно добавить их позже через /setup.")
        await _ask_timezone_method(message, state)
        return

    await state.update_data(
        candidates=candidates,
        excluded_candidate_ids=[],
        prune_page=0,
    )
    await state.set_state(SetupStates.PRUNE_COMPETITORS)

    limit = int(getattr(settings, "MAX_COMPETITORS_YOUTUBE", 20))
    excluded: set[int] = set()
    competitor_rows = [(i, _candidate_display_name(c)) for i, c in enumerate(candidates)]
    selected = len(candidates) - len(excluded)
    text = _build_prune_text(selected=selected, total=len(candidates), limit=limit)
    await message.answer(
        text,
        reply_markup=kb_prune_competitors(competitor_rows=competitor_rows, excluded_ids=excluded, page=0, page_size=8),
    )


@router.callback_query(SetupStates.PRUNE_COMPETITORS, F.data == "noop")
async def on_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(SetupStates.PRUNE_COMPETITORS, F.data.startswith("prune_page:"))
async def on_prune_page(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    try:
        page = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    await state.update_data(prune_page=max(0, page))
    await _render_prune(cb.message, state)


@router.callback_query(SetupStates.PRUNE_COMPETITORS, F.data.startswith("prune_toggle:"))
async def on_prune_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    try:
        cid = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    data = await state.get_data()
    excluded = set(int(x) for x in (data.get("excluded_candidate_ids") or []))
    if cid in excluded:
        excluded.remove(cid)
    else:
        excluded.add(cid)
    await state.update_data(excluded_candidate_ids=sorted(excluded))
    await _render_prune(cb.message, state)


@router.callback_query(SetupStates.PRUNE_COMPETITORS, F.data == "prune_all")
async def on_prune_all(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    await state.update_data(excluded_candidate_ids=[])
    await _render_prune(cb.message, state)


@router.callback_query(SetupStates.PRUNE_COMPETITORS, F.data == "prune_done")
async def on_prune_done(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return

    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    candidates = list(data.get("candidates") or [])
    excluded = set(int(x) for x in (data.get("excluded_candidate_ids") or []))

    selected_indices = [i for i in range(len(candidates)) if i not in excluded]
    if not selected_indices:
        await cb.answer("Нужно оставить хотя бы одного конкурента.", show_alert=True)
        return

    limit = int(getattr(settings, "MAX_COMPETITORS_YOUTUBE", 20))
    if len(selected_indices) > limit:
        await cb.answer(f"Слишком много конкурентов. Исключи еще: {len(selected_indices) - limit}.", show_alert=True)
        return

    # Persist competitors.
    await db_run(
        lambda: Competitor.objects.filter(user=user, platform=Platform.YOUTUBE).update(is_active=False)
    )
    for i in selected_indices:
        c = candidates[i]
        meta = c.get("meta") if isinstance(c.get("meta"), dict) else {}
        await db_call(
            upsert_competitor,
            user=user,
            platform=Platform.YOUTUBE,
            external_id=str(c.get("external_id") or ""),
            handle=c.get("handle"),
            url=str(c.get("url") or ""),
            display_name=c.get("display_name"),
            added_by=str(c.get("added_by") or AddedBy.AUTO),
            meta=meta or None,
        )

    await _ask_timezone_method(cb.message, state)


async def _render_prune(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    candidates = list(data.get("candidates") or [])
    excluded = set(int(x) for x in (data.get("excluded_candidate_ids") or []))
    page = int(data.get("prune_page") or 0)

    limit = int(getattr(settings, "MAX_COMPETITORS_YOUTUBE", 20))
    competitor_rows = [(i, _candidate_display_name(c)) for i, c in enumerate(candidates)]
    selected = len(candidates) - len(excluded)
    text = _build_prune_text(selected=selected, total=len(candidates), limit=limit)
    kb = kb_prune_competitors(competitor_rows=competitor_rows, excluded_ids=excluded, page=page, page_size=8)
    try:
        await message.edit_text(text, reply_markup=kb)
    except Exception:
        # "message is not modified" etc.
        await message.edit_reply_markup(reply_markup=kb)


async def _ask_timezone_method(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    await state.set_state(SetupStates.ASK_TIMEZONE_METHOD)
    await message.answer(
        "В каком часовом поясе отправлять отчеты?\n"
        f"Текущая таймзона: {user.timezone_str}\n\n"
        "Можно отправить геолокацию или ввести вручную.",
        reply_markup=kb_timezone_method(),
    )


@router.message(SetupStates.ASK_TIMEZONE_METHOD, F.location)
async def on_timezone_location(message: Message, state: FSMContext) -> None:
    await state.set_state(SetupStates.WAIT_LOCATION)
    await on_location(message, state)


@router.message(SetupStates.ASK_TIMEZONE_METHOD)
async def on_timezone_method_text(message: Message, state: FSMContext) -> None:
    txt = (message.text or "").strip().lower()
    if "вруч" in txt:
        await state.set_state(SetupStates.WAIT_TZ_MANUAL)
        await message.answer("Введи таймзону: например `Europe/Moscow` или `UTC+03:00`.", reply_markup=ReplyKeyboardRemove())
        return
    if "остав" in txt:
        data = await state.get_data()
        user = await db_call(TgUser.objects.get, id=data["user_id"])
        await _apply_timezone_and_continue(message, state, timezone_str=user.timezone_str, tz_source=user.tz_source)
        return
    await message.answer("Выбери один из вариантов на клавиатуре.")


@router.message(SetupStates.WAIT_LOCATION, F.location)
async def on_location(message: Message, state: FSMContext) -> None:
    lat = message.location.latitude
    lon = message.location.longitude
    tz_name = None
    try:
        from timezonefinder import TimezoneFinder

        tf = TimezoneFinder()
        tz_name = tf.timezone_at(lat=lat, lng=lon)
    except Exception as e:
        logger.warning("timezonefinder failed: %s", e)
        tz_name = None

    if not tz_name:
        await state.set_state(SetupStates.WAIT_TZ_MANUAL)
        await message.answer(
            "Не смог определить таймзону по геолокации. Введи вручную: Europe/Moscow или UTC+03:00.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await _apply_timezone_and_continue(message, state, timezone_str=tz_name, tz_source=TzSource.LOCATION)


@router.message(SetupStates.WAIT_TZ_MANUAL)
async def on_tz_manual(message: Message, state: FSMContext) -> None:
    raw = message.text or ""
    try:
        tz_str = normalize_timezone_str(raw)
    except TimezoneParseError as e:
        await message.answer(str(e))
        return
    await _apply_timezone_and_continue(message, state, timezone_str=tz_str, tz_source=TzSource.MANUAL)


async def _apply_timezone_and_continue(message: Message, state: FSMContext, *, timezone_str: str, tz_source: str) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    user.timezone_str = timezone_str
    user.tz_source = tz_source
    await db_run(lambda: user.save(update_fields=["timezone_str", "tz_source", "updated_at"]))
    await _ask_reports_per_day(message, state)


async def _ask_reports_per_day(message: Message, state: FSMContext) -> None:
    await state.set_state(SetupStates.ASK_REPORTS_PER_DAY)
    await message.answer("Как часто присылать отчет?", reply_markup=kb_reports_per_day())


@router.callback_query(SetupStates.ASK_REPORTS_PER_DAY, F.data.in_(["rpd_1", "rpd_2"]))
async def on_reports_per_day(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    rpd = 1 if cb.data == "rpd_1" else 2
    await state.update_data(reports_per_day=rpd, times=[])

    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])

    if rpd == 1:
        await state.set_state(SetupStates.PICK_TIME_SINGLE)
        await cb.message.answer(
            f"Когда присылать отчет? (время: {user.timezone_str})",
            reply_markup=kb_time_presets_single(),
        )
        return

    await state.set_state(SetupStates.PICK_TIME_PAIR)
    await cb.message.answer(
        f"Когда присылать отчеты? (время: {user.timezone_str})",
        reply_markup=kb_time_presets_pair(),
    )


@router.callback_query(SetupStates.PICK_TIME_SINGLE, F.data.startswith("time1:"))
async def on_time_single(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    value = str(cb.data).split(":", 1)[1]
    if value == "custom":
        await state.set_state(SetupStates.WAIT_TIME_1)
        await cb.message.answer("Напиши время в формате HH:MM (например, 09:00).")
        return
    await state.update_data(times=[value])
    await _finalize_schedule(cb.message, state)


@router.callback_query(SetupStates.PICK_TIME_PAIR, F.data.startswith("timep:"))
async def on_time_pair(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    value = str(cb.data).split(":", 1)[1]
    if value == "custom":
        await state.set_state(SetupStates.PICK_TIME_CUSTOM_1)
        await cb.message.answer("Выбери время первого отчета:", reply_markup=kb_time_presets_first())
        return
    try:
        a, b = value.split(",", 1)
    except Exception:
        return
    times = sorted({a.strip(), b.strip()})
    if len(times) != 2:
        await cb.message.answer("Времена совпали. Выбери другое сочетание или «Другое время».")
        return
    await state.update_data(times=times)
    await _finalize_schedule(cb.message, state)


@router.callback_query(SetupStates.PICK_TIME_CUSTOM_1, F.data.startswith("time1pick:"))
async def on_time1_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    value = str(cb.data).split(":", 1)[1]
    if value == "manual":
        await state.set_state(SetupStates.WAIT_TIME_1)
        await cb.message.answer("Напиши время первого отчета в формате HH:MM (например, 09:00).")
        return
    await state.update_data(times=[value])
    await state.set_state(SetupStates.PICK_TIME_CUSTOM_2)
    await cb.message.answer("Выбери время второго отчета:", reply_markup=kb_time_presets_second())


@router.callback_query(SetupStates.PICK_TIME_CUSTOM_2, F.data.startswith("time2:"))
async def on_time2_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    value = str(cb.data).split(":", 1)[1]
    if value == "manual":
        await state.set_state(SetupStates.WAIT_TIME_2)
        await cb.message.answer("Напиши время второго отчета в формате HH:MM (например, 21:00).")
        return
    data = await state.get_data()
    times = list(data.get("times") or [])
    times.append(value)
    times = sorted(set(times))
    if len(times) != 2:
        await cb.message.answer("Времена совпали. Выбери другое время второго отчета.")
        return
    await state.update_data(times=times)
    await _finalize_schedule(cb.message, state)


@router.message(SetupStates.WAIT_TIME_1)
async def on_time_1_manual(message: Message, state: FSMContext) -> None:
    try:
        t = parse_hhmm(message.text or "")
    except TimeParseError as e:
        await message.answer(str(e))
        return
    value = f"{t.hour:02d}:{t.minute:02d}"

    data = await state.get_data()
    rpd = int(data.get("reports_per_day") or 1)
    if rpd == 1:
        await state.update_data(times=[value])
        await _finalize_schedule(message, state)
        return

    # rpd==2 manual flow: we are picking the first time.
    await state.update_data(times=[value])
    await state.set_state(SetupStates.PICK_TIME_CUSTOM_2)
    await message.answer("Выбери время второго отчета:", reply_markup=kb_time_presets_second())


@router.message(SetupStates.WAIT_TIME_2)
async def on_time_2_manual(message: Message, state: FSMContext) -> None:
    try:
        t = parse_hhmm(message.text or "")
    except TimeParseError as e:
        await message.answer(str(e))
        return
    value = f"{t.hour:02d}:{t.minute:02d}"

    data = await state.get_data()
    times = list(data.get("times") or [])
    times.append(value)
    times = sorted(set(times))
    if len(times) != 2:
        await message.answer("Времена совпали. Введи другое время второго отчета.")
        return
    await state.update_data(times=times)
    await _finalize_schedule(message, state)


async def _finalize_schedule(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    times = sorted(set(list(data.get("times") or [])))
    if not times:
        times = ["09:00"]

    now = timezone.now()
    next_run_at = compute_next_run_at(user.timezone_str, times, now)

    await db_call(
        Schedule.objects.update_or_create,
        user=user,
        defaults={
            "is_enabled": True,
            "times": times,
            "next_run_at": next_run_at,
            "last_run_at": None,
        },
    )

    bootstrap_user_data.delay(user.id)

    comp_count = await db_run(lambda: user.competitors.filter(platform=Platform.YOUTUBE, is_active=True).count())
    await state.clear()

    await message.answer(
        "Готово.\n\n"
        f"Конкуренты (YouTube): {comp_count}\n"
        f"Расписание: {', '.join(times)} (время: {user.timezone_str})\n"
        f"Следующий отчет: {format_dt_local(next_run_at, user.timezone_str)}\n\n"
        "Первый отчет может быть менее точным: для дельт по просмотрам нужен хотя бы один предыдущий снимок метрик.",
        reply_markup=kb_report_now(),
    )
