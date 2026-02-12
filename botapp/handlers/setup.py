from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict
from datetime import timedelta
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from django.conf import settings
from django.utils import timezone

from botapp.db import db_call, db_run
from botapp.keyboards import (
    kb_competitors_next,
    kb_competitors_next_or_ignore,
    kb_prune_competitors,
    kb_prune_keywords,
    kb_reports_per_day,
    kb_seed_candidates,
    kb_time_presets_first,
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
    format_timezone_label,
    normalize_timezone_str,
    parse_hhmm,
)
from tracking.adapters.base import SeedResolution
from tracking.models import AddedBy, Competitor, Platform, Schedule, SeedProfile, SeedStatus, TgUser, TzSource, UserCompetitor
from tracking.services.competitor_service import upsert_competitor
from tracking.adapters.youtube import extract_handle as yt_extract_handle
from tracking.services.llm_usage import decide_and_consume_llm_call
from tracking.services.niche_service import infer_niche_keywords
from tracking.services.youtube_service import (
    YouTubeNotConfigured,
    discover_youtube_competitors,
    resolve_youtube_seed,
    search_youtube_seed_candidates,
)
from tracking.tasks import run_user_report_now

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


def _kw_key(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _merge_keywords(*, new: list[str], existing: list[str], max_keywords: int = 12) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for kw in (new or []) + (existing or []):
        k = re.sub(r"\s+", " ", (kw or "").strip())
        if not k:
            continue
        key = _kw_key(k)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(k)
        if len(out) >= max_keywords:
            break
    return out


def _detect_platform_from_url(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s.lower().startswith(("http://", "https://")):
        return None
    try:
        u = urlparse(s)
    except Exception:
        return None
    host = (u.netloc or "").lower()
    if not host:
        return None
    if "youtube." in host or "youtu.be" in host:
        return Platform.YOUTUBE
    if "instagram." in host:
        return Platform.INSTAGRAM
    if "tiktok." in host:
        return Platform.TIKTOK
    return None


def _seed_from_profile_url(*, platform: str, url: str) -> SeedResolution:
    handle = None
    try:
        u = urlparse(url)
        parts = [p for p in (u.path or "").split("/") if p]
    except Exception:
        parts = []

    if platform == Platform.INSTAGRAM:
        if parts and parts[0] not in {"p", "reel", "tv", "stories"}:
            handle = parts[0]
    elif platform == Platform.TIKTOK:
        if parts:
            first = parts[0]
            if first.startswith("@") and len(first) > 1:
                handle = first[1:]
            elif first and first not in {"t"}:
                handle = first

    return SeedResolution(
        platform=platform,
        external_id="",
        handle=handle,
        url=url,
        title=None,
        description=None,
        uploads_playlist_id=None,
    )


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


def _build_keywords_edit_text(*, keywords: list[str], excluded: set[str]) -> str:
    total = len(keywords)
    included = sum(1 for k in keywords if _kw_key(k) not in excluded)
    lines = ["Ключевые слова по нише."]
    if total:
        lines.append(f"Выбрано: {included}/{total}.")
    lines.append("")
    lines.append("Нажимай на ключевые слова ниже, чтобы исключить лишнее. Можно добавить свои.")
    return "\n".join(lines)


async def _ask_seed(message: Message, state: FSMContext) -> None:
    await state.set_state(SetupStates.WAIT_SEED_INPUT)
    await message.answer("Пришли ссылку или хендл/никнейм профиля.")


async def _enter_competitor_step(
    message: Message,
    state: FSMContext,
    *,
    seed_profile_id: int,
    seed: SeedResolution,
) -> None:
    await state.update_data(
        seed_profile_id=seed_profile_id,
        seed=_seed_to_dict(seed),
        competitor_seeds=[],
    )
    await state.set_state(SetupStates.WAIT_COMPETITOR_LIST)
    await message.answer(
        "Если хочешь, пришли конкурентов: ссылки или хендлы/никнеймы, по одному в строке.\n"
        "Это опционально: если ничего не пришлешь, я сам подберу.",
        reply_markup=kb_competitors_next(),
    )


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
    await db_run(lambda: UserCompetitor.objects.filter(user=user, competitor__platform=Platform.YOUTUBE).update(is_active=False))

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
        await message.answer("Пришли ссылку или хендл/никнейм.")
        return

    sp = await db_call(
        SeedProfile.objects.create,
        user=user,
        raw_input=raw,
        detected_platform="",
        canonical_url="",
        niche_keywords=[],
        niche_source="manual",
        status=SeedStatus.PENDING,
    )

    # Non-YouTube seed URL: accept and continue (MVP analysis is still YouTube-only).
    url_platform = _detect_platform_from_url(raw)
    if url_platform and url_platform != Platform.YOUTUBE:
        seed = _seed_from_profile_url(platform=url_platform, url=raw)
        await db_run(
            lambda: SeedProfile.objects.filter(id=sp.id).update(
                detected_platform=url_platform,
                canonical_url=seed.url,
                status=SeedStatus.RESOLVED,
            )
        )
        await _enter_competitor_step(message, state, seed_profile_id=sp.id, seed=seed)
        return
    if raw.lower().startswith(("http://", "https://")) and url_platform is None:
        await db_run(lambda: SeedProfile.objects.filter(id=sp.id).update(status=SeedStatus.FAILED))
        await message.answer("Не понял эту ссылку. Пришли ссылку на профиль или хендл/никнейм.")
        return

    try:
        seed = await asyncio.to_thread(resolve_youtube_seed, raw)
    except YouTubeNotConfigured:
        seed = None
    except Exception as e:
        logger.warning("Seed resolve failed: %s", e)
        seed = None

    if seed:
        await db_run(
            lambda: SeedProfile.objects.filter(id=sp.id).update(
                detected_platform=Platform.YOUTUBE,
                canonical_url=seed.url,
                status=SeedStatus.RESOLVED,
            )
        )
        await _enter_competitor_step(message, state, seed_profile_id=sp.id, seed=seed)
        return

    # If input looks like a handle / URL, we require an exact match.
    raw_lower = raw.lower()
    looks_like_exact = bool(yt_extract_handle(raw)) or raw_lower.startswith("http") or "youtube." in raw_lower or "youtu.be" in raw_lower
    if looks_like_exact:
        await db_run(lambda: SeedProfile.objects.filter(id=sp.id).update(status=SeedStatus.FAILED))
        await message.answer(
            "Не нашел точного совпадения по этому хендлу/ссылке.\n"
            "Пришли ссылку на профиль или хендл еще раз.",
        )
        return

    # Otherwise treat as nickname and try a best-effort search (user will pick the correct channel).
    try:
        candidates = await asyncio.to_thread(search_youtube_seed_candidates, query=raw, max_results=8)
    except Exception as e:
        logger.warning("Seed search failed: %s", e)
        candidates = []

    if not candidates:
        await db_run(lambda: SeedProfile.objects.filter(id=sp.id).update(status=SeedStatus.FAILED))
        await message.answer(
            "Не нашел профиль по этому никнейму.\n"
            "Пришли ссылку на профиль или хендл.",
        )
        return

    await state.update_data(
        seed_profile_id=sp.id,
        seed_candidates=[_seed_to_dict(c) for c in candidates],
    )
    await state.set_state(SetupStates.PICK_SEED_CANDIDATE)
    await message.answer(
        "Нашел несколько вариантов. Выбери профиль:",
        reply_markup=kb_seed_candidates(candidates=[_seed_to_dict(c) for c in candidates]),
    )


@router.callback_query(SetupStates.PICK_SEED_CANDIDATE, F.data == "seed_retry")
async def on_seed_retry(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    await state.update_data(seed_candidates=[])
    await _ask_seed(cb.message, state)


@router.callback_query(SetupStates.PICK_SEED_CANDIDATE, F.data.startswith("seed_pick:"))
async def on_seed_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    data = await state.get_data()
    try:
        idx = int(str(cb.data).split(":", 1)[1])
    except Exception:
        idx = -1

    candidates = data.get("seed_candidates") or []
    if not (0 <= idx < len(candidates)):
        await cb.message.answer("Не понял выбор. Пришли ссылку или хендл/никнейм еще раз.")
        await _ask_seed(cb.message, state)
        return

    seed = _seed_from_dict(candidates[idx])
    sp_id = int(data.get("seed_profile_id") or 0)
    if sp_id:
        await db_run(
            lambda: SeedProfile.objects.filter(id=sp_id).update(
                detected_platform=Platform.YOUTUBE,
                canonical_url=seed.url,
                status=SeedStatus.RESOLVED,
            )
        )

    await _enter_competitor_step(cb.message, state, seed_profile_id=sp_id, seed=seed)


@router.callback_query(SetupStates.WAIT_COMPETITOR_LIST, F.data.in_(["comp_done", "comp_clear"]))
async def on_competitors_optional(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return

    if cb.data == "comp_clear":
        await state.update_data(competitor_seeds=[])
    await _start_keywords_step(cb.message, state)


@router.message(SetupStates.WAIT_COMPETITOR_LIST)
async def on_competitor_list(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer(
            "Пришли список конкурентов одним сообщением (по одному в строке) или нажми «Дальше».",
            reply_markup=kb_competitors_next(),
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
    kb = kb_competitors_next_or_ignore() if existing else kb_competitors_next()
    await message.answer(" ".join(parts), reply_markup=kb)


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

    # Build a YouTube-only context seed (we can infer niche from it).
    context_seed = seed if (seed and seed.platform == Platform.YOUTUBE and seed.external_id) else None
    if context_seed is None and comp_seeds:
        context_seed = comp_seeds[0]
        comp_seeds = comp_seeds[1:]

    if context_seed is None:
        await state.update_data(niche_keywords=[], excluded_keywords=[])
        await state.set_state(SetupStates.ADD_NICHE)
        await message.answer("Чтобы подобрать конкурентов, напиши ключевые слова по нише (через запятую или с новой строки).")
        return

    await message.answer("Секунду, подбираю ключевые слова по нише…")

    decision = await db_call(decide_and_consume_llm_call, user=user, now_utc=timezone.now())
    if not decision.allow and decision.reason == "limit_reached":
        await message.answer(
            f"Лимит умного анализа на сегодня исчерпан ({decision.used_today}/{decision.max_calls_per_day}). Использую быстрый анализ."
        )
    prefer_llm = decision.allow
    try:
        kws, source = await asyncio.to_thread(
            infer_niche_keywords,
            seed=context_seed,
            competitors=comp_seeds,
            prefer_llm=prefer_llm,
        )
    except Exception as e:
        logger.warning("infer_niche_keywords failed: %s", e)
        kws, source = [], "auto"

    kws = [k.strip() for k in (kws or []) if isinstance(k, str) and k.strip()][:12]
    if not kws:
        await state.update_data(niche_keywords=[], excluded_keywords=[])
        await state.set_state(SetupStates.ADD_NICHE)
        await message.answer("Не получилось надежно определить нишу. Напиши ключевые слова (через запятую или с новой строки).")
        return

    if sp:
        await db_run(
            lambda: SeedProfile.objects.filter(id=sp.id).update(
                niche_keywords=kws,
                niche_source=source,
                status=SeedStatus.RESOLVED,
            )
        )

    await state.update_data(niche_keywords=kws, excluded_keywords=[], niche_source=source)
    await _show_keywords_editor(message, state)


async def _show_keywords_editor(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    keywords = list(data.get("niche_keywords") or [])
    excluded = set(str(x) for x in (data.get("excluded_keywords") or []))
    await state.set_state(SetupStates.EDIT_NICHE)
    m = await message.answer(
        _build_keywords_edit_text(keywords=keywords, excluded=excluded),
        reply_markup=kb_prune_keywords(keywords=keywords, excluded=excluded),
    )
    await state.update_data(kw_editor_chat_id=m.chat.id, kw_editor_message_id=m.message_id)


async def _render_keywords_editor(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    keywords = list(data.get("niche_keywords") or [])
    excluded = set(str(x) for x in (data.get("excluded_keywords") or []))
    text = _build_keywords_edit_text(keywords=keywords, excluded=excluded)
    kb = kb_prune_keywords(keywords=keywords, excluded=excluded)
    try:
        await message.edit_text(text, reply_markup=kb)
    except Exception:
        try:
            await message.edit_reply_markup(reply_markup=kb)
        except Exception:
            # "message is not modified" etc.
            return


@router.callback_query(SetupStates.EDIT_NICHE, F.data.startswith("kw_toggle:"))
async def on_kw_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    try:
        idx = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    data = await state.get_data()
    keywords = list(data.get("niche_keywords") or [])
    if not (0 <= idx < len(keywords)):
        return
    excluded = set(str(x) for x in (data.get("excluded_keywords") or []))
    key = _kw_key(keywords[idx])
    if key in excluded:
        excluded.remove(key)
    else:
        excluded.add(key)
    await state.update_data(excluded_keywords=sorted(excluded))
    await _render_keywords_editor(cb.message, state)


@router.callback_query(SetupStates.EDIT_NICHE, F.data == "kw_all")
async def on_kw_all(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    await state.update_data(excluded_keywords=[])
    await _render_keywords_editor(cb.message, state)


@router.callback_query(SetupStates.EDIT_NICHE, F.data == "kw_add")
async def on_kw_add(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    await state.update_data(kw_editor_chat_id=cb.message.chat.id, kw_editor_message_id=cb.message.message_id)
    await state.set_state(SetupStates.ADD_NICHE)
    prompt = await cb.message.answer(
        "Добавь ключевые слова по нише (через запятую или с новой строки).\n"
        "Они добавятся к текущим.",
    )
    await state.update_data(kw_add_prompt_chat_id=prompt.chat.id, kw_add_prompt_message_id=prompt.message_id)


@router.callback_query(SetupStates.EDIT_NICHE, F.data == "kw_done")
async def on_kw_done(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    data = await state.get_data()
    keywords = list(data.get("niche_keywords") or [])
    excluded = set(str(x) for x in (data.get("excluded_keywords") or []))
    final = [k for k in keywords if _kw_key(k) not in excluded]
    if not final:
        await cb.answer("Нужно оставить хотя бы одно ключевое слово.", show_alert=True)
        return

    sp_id = int(data.get("seed_profile_id") or 0)
    source = str(data.get("niche_source") or "manual")
    if sp_id:
        await db_run(
            lambda: SeedProfile.objects.filter(id=sp_id).update(
                niche_keywords=final,
                niche_source=source,
                status=SeedStatus.RESOLVED,
            )
        )

    await state.update_data(niche_keywords=final)
    await _start_discovery(cb.message, state)


@router.message(SetupStates.ADD_NICHE)
async def on_add_niche(message: Message, state: FSMContext) -> None:
    new_kws = _parse_keywords(message.text or "")
    if not new_kws:
        await message.answer(
            "Не вижу ключевых слов. Пришли через запятую или с новой строки, например:\n"
            "сборка ПК\nкомплектующие\nремонт"
        )
        return

    data = await state.get_data()
    existing = list(data.get("niche_keywords") or [])
    merged = _merge_keywords(new=new_kws, existing=existing, max_keywords=12) if existing else new_kws[:12]
    await state.update_data(niche_keywords=merged)

    # Return to the editor.
    await state.set_state(SetupStates.EDIT_NICHE)
    excluded = set(str(x) for x in (data.get("excluded_keywords") or []))
    text = _build_keywords_edit_text(keywords=merged, excluded=excluded)
    kb = kb_prune_keywords(keywords=merged, excluded=excluded)

    # UX: send a NEW editor message after the user's input (more intuitive than editing older messages above).
    old_editor_chat_id = data.get("kw_editor_chat_id")
    old_editor_msg_id = data.get("kw_editor_message_id")
    prompt_chat_id = data.get("kw_add_prompt_chat_id")
    prompt_msg_id = data.get("kw_add_prompt_message_id")

    m = await message.answer(text, reply_markup=kb)
    await state.update_data(kw_editor_chat_id=m.chat.id, kw_editor_message_id=m.message_id, kw_add_prompt_chat_id=None, kw_add_prompt_message_id=None)
    if old_editor_chat_id and old_editor_msg_id and (
        int(old_editor_chat_id) != int(m.chat.id) or int(old_editor_msg_id) != int(m.message_id)
    ):
        try:
            await message.bot.delete_message(chat_id=int(old_editor_chat_id), message_id=int(old_editor_msg_id))
        except Exception:
            pass
    if prompt_chat_id and prompt_msg_id and (
        int(prompt_chat_id) != int(m.chat.id) or int(prompt_msg_id) != int(m.message_id)
    ):
        try:
            await message.bot.delete_message(chat_id=int(prompt_chat_id), message_id=int(prompt_msg_id))
        except Exception:
            pass


async def _start_discovery(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])

    seed_dict = data.get("seed") or None
    seed = _seed_from_dict(seed_dict) if isinstance(seed_dict, dict) else None
    if seed and (seed.platform != Platform.YOUTUBE or not seed.external_id):
        seed = None

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
        lambda: UserCompetitor.objects.filter(user=user, competitor__platform=Platform.YOUTUBE).update(is_active=False)
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
        try:
            await message.edit_reply_markup(reply_markup=kb)
        except Exception:
            # "message is not modified" etc.
            return


async def _ask_timezone_method(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    tz_label = format_timezone_label(user.timezone_str)
    await state.set_state(SetupStates.ASK_TIMEZONE_METHOD)
    await message.answer(
        "В каком часовом поясе отправлять отчеты?\n"
        f"Текущая таймзона: {tz_label}\n\n"
        "Можно отправить геолокацию или ввести вручную.",
        reply_markup=kb_timezone_method(),
    )

@router.callback_query(SetupStates.ASK_TIMEZONE_METHOD, F.data.in_(["tz_location", "tz_manual", "tz_keep"]))
async def on_timezone_method_choice(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return

    # Remove the choice keyboard after click.
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if cb.data == "tz_manual":
        await state.set_state(SetupStates.WAIT_TZ_MANUAL)
        await cb.message.answer(
            "Введи таймзону: например `Europe/Moscow` или `UTC+03:00`.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if cb.data == "tz_keep":
        data = await state.get_data()
        user = await db_call(TgUser.objects.get, id=data["user_id"])
        await _apply_timezone_and_continue(cb.message, state, timezone_str=user.timezone_str, tz_source=user.tz_source)
        return

    # tz_location
    await state.set_state(SetupStates.WAIT_LOCATION)
    await cb.message.answer(
        "Отправь геолокацию одним сообщением.\n"
        "В Telegram: скрепка → Геопозиция.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(SetupStates.ASK_TIMEZONE_METHOD, F.location)
async def on_timezone_location(message: Message, state: FSMContext) -> None:
    await state.set_state(SetupStates.WAIT_LOCATION)
    await on_location(message, state)


@router.message(SetupStates.ASK_TIMEZONE_METHOD)
async def on_timezone_method_text(message: Message) -> None:
    await message.answer("Нажми одну из кнопок ниже.")


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
    tz_label = format_timezone_label(user.timezone_str)

    if rpd == 1:
        await state.set_state(SetupStates.PICK_TIME_SINGLE)
        await cb.message.answer(
            f"Когда присылать отчет?\nВремя: {tz_label}",
            reply_markup=kb_time_presets_single(),
        )
        return

    await state.set_state(SetupStates.PICK_TIME_CUSTOM_1)
    await cb.message.answer(
        f"Когда присылать первый отчет?\nВремя: {tz_label}",
        reply_markup=kb_time_presets_first(),
    )


@router.callback_query(SetupStates.PICK_TIME_SINGLE, F.data.startswith("time1:"))
async def on_time_single(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    value = str(cb.data).split(":", 1)[1]
    if value == "custom":
        data = await state.get_data()
        user = await db_call(TgUser.objects.get, id=data["user_id"])
        tz_label = format_timezone_label(user.timezone_str)
        await state.set_state(SetupStates.WAIT_TIME_1)
        await cb.message.answer(f"Напиши время в формате HH:MM (например, 09:00).\nВремя: {tz_label}")
        return
    await state.update_data(times=[value])
    await _finalize_schedule(cb.message, state)


@router.callback_query(SetupStates.PICK_TIME_CUSTOM_1, F.data.startswith("time1pick:"))
async def on_time1_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    value = str(cb.data).split(":", 1)[1]
    if value == "manual":
        data = await state.get_data()
        user = await db_call(TgUser.objects.get, id=data["user_id"])
        tz_label = format_timezone_label(user.timezone_str)
        await state.set_state(SetupStates.WAIT_TIME_1)
        await cb.message.answer(f"Напиши время первого отчета в формате HH:MM (например, 09:00).\nВремя: {tz_label}")
        return
    await state.update_data(times=[value])
    await state.set_state(SetupStates.PICK_TIME_CUSTOM_2)
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    tz_label = format_timezone_label(user.timezone_str)
    await cb.message.answer(f"Когда присылать второй отчет?\nВремя: {tz_label}", reply_markup=kb_time_presets_second())


@router.callback_query(SetupStates.PICK_TIME_CUSTOM_2, F.data.startswith("time2:"))
async def on_time2_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    value = str(cb.data).split(":", 1)[1]
    if value == "manual":
        data = await state.get_data()
        user = await db_call(TgUser.objects.get, id=data["user_id"])
        tz_label = format_timezone_label(user.timezone_str)
        await state.set_state(SetupStates.WAIT_TIME_2)
        await cb.message.answer(f"Напиши время второго отчета в формате HH:MM (например, 21:00).\nВремя: {tz_label}")
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
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    tz_label = format_timezone_label(user.timezone_str)
    await message.answer(f"Когда присылать второй отчет?\nВремя: {tz_label}", reply_markup=kb_time_presets_second())


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
    tz_label = format_timezone_label(user.timezone_str)
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

    comp_count = await db_run(
        lambda: UserCompetitor.objects.filter(user=user, is_active=True, competitor__platform=Platform.YOUTUBE).count()
    )
    await state.clear()

    await message.answer(
        "Готово.\n\n"
        f"Конкуренты (YouTube): {comp_count}\n"
        f"Расписание: {', '.join(times)}\n"
        f"Время: {tz_label}\n"
        f"Следующий отчет: {format_dt_local(next_run_at, user.timezone_str)}\n\n"
        "Сейчас соберу первый отчет, чтобы все проверить.",
    )

    run_user_report_now.delay(user.id)
