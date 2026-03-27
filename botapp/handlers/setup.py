from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict
from datetime import timedelta
from urllib.parse import urlparse
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from django.conf import settings
from django.utils import timezone

from botapp.db import db_call, db_run
from botapp.user_sync import upsert_tg_user
from botapp.keyboards import (
    kb_competitors_next,
    kb_competitors_next_or_ignore,
    kb_link_candidates,
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
from tracking.models import (
    AddedBy,
    LinkedAccountSource,
    Platform,
    Schedule,
    SeedProfile,
    SeedStatus,
    TgUser,
    TzSource,
    UserLinkedAccount,
    UserCompetitor,
)
from tracking.services.account_linking import replace_user_linked_accounts, suggest_accounts_for_platforms
from tracking.services.competitor_service import upsert_competitor
from tracking.services.niche_service import infer_niche_keywords
from tracking.services.platform_onboarding import (
    PlatformOnboardingError,
    build_search_ready_keywords,
    discover_competitors_for_onboarding,
)
from tracking.services.seed_resolver import (
    SeedResolveAmbiguity,
    SeedResolveError,
    SeedResolveAttempt,
    can_search_youtube_seed_candidates,
    candidate_platforms_for_exact_seed,
    attempt_exact_seed_resolution,
    resolve_exact_seed,
    resolve_seed_for_platform,
)
from tracking.services.setup_runtime import (
    PLATFORM_STATE_AVAILABLE,
    PLATFORM_STATE_ERROR,
    PLATFORM_STATE_UNAVAILABLE,
    SetupRunContext,
    get_platform_state,
)
from tracking.services.youtube_service import search_youtube_seed_candidates
from tracking.tasks import run_user_report_now

logger = logging.getLogger(__name__)
router = Router()
_SETUP_RUNTIMES: dict[str, SetupRunContext] = {}


def _candidate_key(seed: SeedResolution | dict) -> str:
    if isinstance(seed, dict):
        platform = str(seed.get("platform") or "")
        external_id = str(seed.get("external_id") or "")
    else:
        platform = str(seed.platform or "")
        external_id = str(seed.external_id or "")
    return f"{platform}:{external_id}"


def _build_active_competitor_summary(*, counts: dict[str, int]) -> list[str]:
    return [
        f"YouTube: {counts.get(Platform.YOUTUBE, 0)}",
        f"TikTok: {counts.get(Platform.TIKTOK, 0)}",
        f"Instagram: {counts.get(Platform.INSTAGRAM, 0)}",
    ]


def _seed_to_dict(seed: SeedResolution) -> dict:
    return asdict(seed)


def _seed_from_dict(data: dict) -> SeedResolution:
    try:
        subscriber_count = int(data.get("subscriber_count")) if data.get("subscriber_count") is not None else None
    except Exception:
        subscriber_count = None
    return SeedResolution(
        platform=str(data.get("platform") or ""),
        external_id=str(data.get("external_id") or ""),
        handle=data.get("handle"),
        url=str(data.get("url") or ""),
        title=data.get("title"),
        description=data.get("description"),
        uploads_playlist_id=data.get("uploads_playlist_id"),
        subscriber_count=subscriber_count,
    )


def _platform_label(platform: str | None) -> str:
    return {
        Platform.YOUTUBE: "YouTube",
        Platform.TIKTOK: "TikTok",
        Platform.INSTAGRAM: "Instagram",
    }.get(str(platform or ""), str(platform or "Platform"))


def _new_setup_runtime_id() -> str:
    runtime_id = uuid4().hex
    _SETUP_RUNTIMES[runtime_id] = SetupRunContext()
    return runtime_id


def _get_setup_runtime(data: dict) -> SetupRunContext:
    runtime_id = str(data.get("setup_runtime_id") or "").strip()
    if not runtime_id:
        runtime_id = _new_setup_runtime_id()
        data["setup_runtime_id"] = runtime_id
    return _SETUP_RUNTIMES.setdefault(runtime_id, SetupRunContext())


async def _clear_setup_runtime(state: FSMContext) -> None:
    data = await state.get_data()
    runtime_id = str(data.get("setup_runtime_id") or "").strip()
    if runtime_id:
        _SETUP_RUNTIMES.pop(runtime_id, None)


def _platform_unavailable_message(*, platform: str, reason: str, continuation: str) -> str:
    return f"{_platform_label(platform)} временно недоступен: {reason}. {continuation}"


def _manual_niche_fallback_message(*, platform: str, reason: str) -> str:
    return (
        f"{_platform_label(platform)} сейчас недоступен, поэтому я не могу подтвердить этот профиль автоматически.\n"
        f"Причина: {reason}\n\n"
        "Можем продолжить в сокращенном режиме: введи ключевые слова по нише вручную."
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


def _linked_account_to_dict(
    seed: SeedResolution,
    *,
    source: str,
    signals: list[str] | None = None,
    is_seed: bool = False,
) -> dict:
    data = _seed_to_dict(seed)
    data["source"] = source
    data["signals"] = list(signals or [])
    data["is_seed"] = is_seed
    data["meta"] = {
        "description": str(seed.description or ""),
        "uploads_playlist_id": str(seed.uploads_playlist_id or ""),
    }
    return data


def _format_match_signal(signal: str) -> str:
    raw = str(signal or "").strip()
    if raw == "exact_handle":
        return "совпал хендл"
    if raw == "normalized_handle":
        return "совпал нормализованный хендл"
    if raw.startswith("display_similarity:"):
        try:
            ratio = float(raw.split(":", 1)[1])
        except Exception:
            return "похожее название профиля"
        if ratio >= 0.92:
            return "название профиля почти совпало"
        return "название профиля похоже"
    if raw.startswith("shared_tokens:"):
        tokens = [token for token in raw.split(":", 1)[1].split(",") if token]
        if tokens:
            return "совпали слова в имени/био: " + ", ".join(tokens[:3])
    if raw.startswith("shared_domains:"):
        domains = [domain for domain in raw.split(":", 1)[1].split(",") if domain]
        if domains:
            return "совпали внешние домены: " + ", ".join(domains[:2])
    if raw == "seed_exact_resolve":
        return "исходный профиль подтвержден точно"
    if raw == "manual_input":
        return "подтверждено вручную"
    if raw == "provider_hint":
        return "есть явная подсказка из профиля/ссылок"
    return raw


def _build_link_prompt(*, platform: str, candidates: list[dict], note: str | None) -> str:
    label = _platform_label(platform)
    lines = [f"Проверяю, есть ли у тебя {label}.", "Я не связываю аккаунты автоматически без подтверждения."]
    if candidates:
        lines.append("")
        for idx, candidate in enumerate(candidates, start=1):
            title = str(candidate.get("title") or candidate.get("handle") or candidate.get("external_id") or "Без названия")
            handle = str(candidate.get("handle") or "").strip()
            url = str(candidate.get("url") or "").strip()
            signals = [_format_match_signal(item) for item in (candidate.get("signals") or []) if str(item).strip()]
            line = f"{idx}. {title}"
            if handle:
                line += f" (@{handle})"
            lines.append(line)
            if url:
                lines.append(url)
            if signals:
                lines.append("Сигналы: " + "; ".join(signals) + ".")
            lines.append("")
        lines.append(f"Это ваш {label}? Выбери вариант ниже. Если это не тот аккаунт, введи {label} вручную или пропусти.")
        return "\n".join(lines)

    lines.append("")
    if note:
        lines.append(f"Автопоиск не дал подтвержденного совпадения.\nПричина: {note}")
    else:
        lines.append("Автопоиск не дал подтвержденного совпадения.")
    lines.append(f"Пришли ссылку или хендл {label} вручную, либо нажми «Пропустить».")
    return "\n".join(lines)


def _manual_link_prompt(platform: str) -> str:
    label = _platform_label(platform)
    examples = {
        Platform.YOUTUBE: "https://www.youtube.com/@creator или @creator",
        Platform.TIKTOK: "https://www.tiktok.com/@creator или @creator",
        Platform.INSTAGRAM: "https://www.instagram.com/creator/ или creator",
    }
    return f"Пришли ссылку или хендл для {label}.\nНапример: {examples.get(platform, '@creator')}"


def _build_linked_accounts_summary(*, linked_accounts: dict[str, dict], skipped_platforms: list[str]) -> str:
    lines = ["Подтвердил профили для дальнейшего анализа:"]
    for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM):
        account = linked_accounts.get(platform) if isinstance(linked_accounts, dict) else None
        if account:
            title = str(account.get("title") or account.get("handle") or account.get("external_id") or "Без названия")
            handle = str(account.get("handle") or "").strip()
            suffix = f" (@{handle})" if handle else ""
            lines.append(f"{_platform_label(platform)}: {title}{suffix}")
            continue
        if platform in skipped_platforms:
            lines.append(f"{_platform_label(platform)}: не подтвержден")
        else:
            lines.append(f"{_platform_label(platform)}: не задан")
    return "\n".join(lines)


def _candidate_display_name(c: dict) -> str:
    platform = str(c.get("platform") or "").strip()
    platform_label = _platform_label(platform)
    name = (c.get("display_name") or c.get("handle") or c.get("external_id") or "").strip() or "Без названия"
    return f"[{platform_label}] {name}"


def _build_prune_text(
    *,
    selected_total: int,
    selected_by_platform: dict[str, int],
    limit: int,
    discovery_notes: list[str],
) -> str:
    total_limit = limit * 3
    lines = [
        "Нашел конкурентов.",
        "Нажимай на профили, чтобы исключить лишних. По умолчанию выбраны все.",
        f"Выбрано всего: {selected_total}/{total_limit}.",
        f"YouTube: {selected_by_platform.get(Platform.YOUTUBE, 0)}/{limit}.",
        f"TikTok: {selected_by_platform.get(Platform.TIKTOK, 0)}/{limit}.",
        f"Instagram: {selected_by_platform.get(Platform.INSTAGRAM, 0)}/{limit}.",
    ]
    for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM):
        selected = selected_by_platform.get(platform, 0)
        if selected > limit:
            lines.append(f"Нужно исключить {_platform_label(platform)}-конкурентов еще: {selected - limit}.")
    if discovery_notes:
        lines.append("")
        lines.extend(discovery_notes)
    lines.append("Когда готово, нажми «Готово».")
    return "\n".join(lines)


def _selected_counts_by_platform(*, candidates: list[dict], excluded: set[int]) -> dict[str, int]:
    return {
        platform: sum(
            1
            for idx, candidate in enumerate(candidates)
            if idx not in excluded and str(candidate.get("platform") or "") == platform
        )
        for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM)
    }


def _quota_error_text(*, selected_by_platform: dict[str, int], limit: int) -> str | None:
    overflow: list[str] = []
    for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM):
        selected = selected_by_platform.get(platform, 0)
        if selected > limit:
            overflow.append(f"{_platform_label(platform)}: исключи еще {selected - limit}")
    if not overflow:
        return None
    return "Слишком много выбранных конкурентов.\n" + "\n".join(overflow)


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


async def _persist_linked_accounts(state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    linked_accounts = data.get("linked_accounts") or {}
    await db_run(lambda: replace_user_linked_accounts(user=user, accounts_by_platform=linked_accounts))


async def _enter_manual_niche_mode(
    message: Message,
    state: FSMContext,
    *,
    seed_profile_id: int,
    notice: str,
) -> None:
    await state.update_data(
        seed_profile_id=seed_profile_id,
        seed=None,
        linked_accounts={},
        link_platform_queue=[],
        link_suggestions={},
        skipped_link_platforms=[],
        competitor_seeds=[],
        niche_keywords=[],
        excluded_keywords=[],
        niche_source="manual",
    )
    await state.set_state(SetupStates.ADD_NICHE)
    await message.answer(notice)


def _seed_from_linked_account_row(linked: UserLinkedAccount) -> SeedResolution:
    meta = linked.meta if isinstance(linked.meta, dict) else {}
    uploads_playlist_id = str(meta.get("uploads_playlist_id") or "").strip() or None
    description = str(meta.get("description") or "").strip() or None
    return SeedResolution(
        platform=linked.platform,
        external_id=linked.external_id,
        handle=linked.handle or None,
        url=linked.url or "",
        title=linked.display_name or None,
        description=description,
        uploads_playlist_id=uploads_playlist_id,
    )


async def _load_confirmed_linked_accounts(*, user: TgUser, state: FSMContext) -> list[SeedResolution]:
    data = await state.get_data()
    linked_accounts = data.get("linked_accounts") or {}
    state_accounts: list[SeedResolution] = []
    for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM):
        raw = linked_accounts.get(platform) if isinstance(linked_accounts, dict) else None
        if not isinstance(raw, dict):
            continue
        try:
            account = _seed_from_dict(raw)
        except Exception:
            continue
        if account.external_id:
            state_accounts.append(account)
    if state_accounts:
        return state_accounts

    rows = await db_run(
        lambda: list(
            UserLinkedAccount.objects.filter(user=user)
            .order_by("-is_seed", "platform")
        )
    )
    return [_seed_from_linked_account_row(row) for row in rows if row.external_id]


async def _ask_next_linked_account(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    queue = [str(item) for item in (data.get("link_platform_queue") or []) if str(item).strip()]
    seed_dict = data.get("seed") or None
    seed = _seed_from_dict(seed_dict) if isinstance(seed_dict, dict) else None
    if seed is None:
        await message.answer("Потерял подтвержденный исходный профиль. Запусти /setup еще раз.")
        return

    suggestions_by_platform = dict(data.get("link_suggestions") or {})
    skipped_platforms = [str(item) for item in (data.get("skipped_link_platforms") or []) if str(item).strip()]
    unavailable_notes: list[str] = []
    while queue:
        current_platform = queue[0]
        suggestion = suggestions_by_platform.get(current_platform)
        status = str((suggestion or {}).get("status") or PLATFORM_STATE_AVAILABLE)
        note = str((suggestion or {}).get("note") or "").strip()
        if status not in {PLATFORM_STATE_UNAVAILABLE, PLATFORM_STATE_ERROR}:
            break
        if current_platform not in skipped_platforms:
            skipped_platforms.append(current_platform)
        queue = queue[1:]
        unavailable_notes.append(
            _platform_unavailable_message(
                platform=current_platform,
                reason=note or "провайдер недоступен",
                continuation=f"Продолжаю без {_platform_label(current_platform)}.",
            )
        )

    if unavailable_notes:
        await state.update_data(link_platform_queue=queue, skipped_link_platforms=skipped_platforms)
        for note in unavailable_notes:
            await message.answer(note)

    if not queue:
        await _persist_linked_accounts(state)
        linked_accounts = data.get("linked_accounts") or {}
        await message.answer(_build_linked_accounts_summary(linked_accounts=linked_accounts, skipped_platforms=skipped_platforms))
        await _enter_competitor_step(
            message,
            state,
            seed_profile_id=int(data.get("seed_profile_id") or 0),
            seed=seed,
        )
        return

    platform = queue[0]
    suggestion = suggestions_by_platform.get(platform)
    if not isinstance(suggestion, dict):
        suggestion = {"candidates": [], "note": "Не удалось подготовить подсказки для этой платформы."}

    await state.update_data(current_link_platform=platform)
    await state.set_state(SetupStates.PICK_LINKED_ACCOUNT)
    await message.answer(
        _build_link_prompt(
            platform=platform,
            candidates=list(suggestion.get("candidates") or []),
            note=str(suggestion.get("note") or "").strip() or None,
        ),
        reply_markup=kb_link_candidates(candidates=list(suggestion.get("candidates") or [])),
    )


async def _begin_account_linking(
    message: Message,
    state: FSMContext,
    *,
    seed_profile_id: int,
    seed: SeedResolution,
) -> None:
    data = await state.get_data()
    runtime = _get_setup_runtime(data)
    target_platforms = [platform for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM) if platform != seed.platform]
    raw_suggestions = await asyncio.to_thread(
        suggest_accounts_for_platforms,
        seed=seed,
        target_platforms=target_platforms,
        max_candidates=3,
        context=runtime,
    )
    link_suggestions = {
        platform: {
            "candidates": [
                _linked_account_to_dict(candidate.seed, source=LinkedAccountSource.AUTO, signals=candidate.signals)
                for candidate in suggestion.candidates
            ],
            "note": suggestion.note,
            "status": suggestion.status,
        }
        for platform, suggestion in raw_suggestions.items()
    }
    linked_accounts = {
        seed.platform: _linked_account_to_dict(
            seed,
            source=LinkedAccountSource.SEED,
            signals=["seed_exact_resolve"],
            is_seed=True,
        )
    }
    queue = list(target_platforms)
    await state.update_data(
        seed_profile_id=seed_profile_id,
        seed=_seed_to_dict(seed),
        linked_accounts=linked_accounts,
        link_platform_queue=queue,
        link_suggestions=link_suggestions,
        skipped_link_platforms=[],
        competitor_seeds=[],
    )
    await message.answer("Подтверждаю аккаунты на остальных платформах, чтобы связать их в один набор.")
    await _ask_next_linked_account(message, state)


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
        "Если не пришлешь список, я попробую предложить конкурентов автоматически там, где платформа реально это поддерживает.",
        reply_markup=kb_competitors_next(),
    )


@router.message(Command("setup"))
async def cmd_setup(message: Message, state: FSMContext) -> None:
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

    # Reset active competitors for a clean re-setup.
    await db_run(lambda: UserCompetitor.objects.filter(user=user).update(is_active=False))

    await _clear_setup_runtime(state)
    await state.clear()
    await state.update_data(user_id=user.id, setup_runtime_id=_new_setup_runtime_id())
    await _ask_seed(message, state)


@router.message(Command("schedule"))
async def cmd_schedule(message: Message, state: FSMContext) -> None:
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
    await _clear_setup_runtime(state)
    await state.clear()
    await state.update_data(user_id=user.id, setup_runtime_id=_new_setup_runtime_id())
    await _ask_timezone_method(message, state)


@router.message(SetupStates.WAIT_SEED_INPUT)
async def on_seed_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    runtime = _get_setup_runtime(data)
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

    url_platform = _detect_platform_from_url(raw)
    if raw.lower().startswith(("http://", "https://")) and url_platform is None:
        await db_run(lambda: SeedProfile.objects.filter(id=sp.id).update(status=SeedStatus.FAILED))
        await message.answer("Не понял эту ссылку. Пришли ссылку на профиль или хендл/никнейм.")
        return

    if url_platform:
        try:
            seed = await asyncio.to_thread(resolve_seed_for_platform, platform=url_platform, raw_input=raw, context=runtime)
        except SeedResolveError as e:
            platform_state = get_platform_state(runtime, url_platform)
            if platform_state.state in {PLATFORM_STATE_UNAVAILABLE, PLATFORM_STATE_ERROR}:
                await db_run(
                    lambda: SeedProfile.objects.filter(id=sp.id).update(
                        detected_platform=url_platform,
                        status=SeedStatus.PENDING,
                    )
                )
                await _enter_manual_niche_mode(
                    message,
                    state,
                    seed_profile_id=sp.id,
                    notice=_manual_niche_fallback_message(platform=url_platform, reason=str(e)),
                )
                return
            await db_run(lambda: SeedProfile.objects.filter(id=sp.id).update(status=SeedStatus.FAILED))
            await message.answer("Не получилось подтвердить профиль.\n" f"Причина: {e}")
            return
        except Exception as e:
            logger.warning("Seed resolve failed: %s", e)
            await db_run(lambda: SeedProfile.objects.filter(id=sp.id).update(status=SeedStatus.FAILED))
            await message.answer("Не получилось подтвердить профиль.\n" f"Причина: {e}")
            return
    else:
        try:
            seed = await asyncio.to_thread(resolve_exact_seed, raw, context=runtime)
        except SeedResolveAmbiguity as e:
            candidates = [_seed_to_dict(candidate) for candidate in e.candidates]
            await state.update_data(seed_profile_id=sp.id, seed_candidates=candidates)
            await state.set_state(SetupStates.PICK_SEED_CANDIDATE)
            note = ""
            if e.errors:
                note = "\n\nНе все платформы удалось проверить:\n" + "\n".join(f"- {error}" for error in e.errors[:2])
            await message.answer(
                "Нашел точные совпадения на нескольких платформах. Выбери нужный профиль:" + note,
                reply_markup=kb_seed_candidates(candidates=candidates),
            )
            return
        except SeedResolveError as e:
            await db_run(lambda: SeedProfile.objects.filter(id=sp.id).update(status=SeedStatus.FAILED))
            await message.answer("Не получилось подтвердить профиль.\n" f"Причина: {e}")
            return
        except Exception as e:
            logger.warning("Seed resolve failed: %s", e)
            await db_run(lambda: SeedProfile.objects.filter(id=sp.id).update(status=SeedStatus.FAILED))
            await message.answer("Не получилось подтвердить профиль.\n" f"Причина: {e}")
            return

    if seed:
        await db_run(
            lambda: SeedProfile.objects.filter(id=sp.id).update(
                detected_platform=seed.platform,
                canonical_url=seed.url,
                status=SeedStatus.RESOLVED,
            )
        )
        await _begin_account_linking(message, state, seed_profile_id=sp.id, seed=seed)
        return

    if not can_search_youtube_seed_candidates(raw):
        await db_run(lambda: SeedProfile.objects.filter(id=sp.id).update(status=SeedStatus.FAILED))
        await message.answer(
            "Не нашел точного совпадения по этому хендлу/ссылке.\n"
            "Если одинаковый хендл есть на разных платформах, пришли полную ссылку на нужный профиль.",
        )
        return

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
                detected_platform=seed.platform,
                canonical_url=seed.url,
                status=SeedStatus.RESOLVED,
            )
        )

    await _begin_account_linking(cb.message, state, seed_profile_id=sp_id, seed=seed)


@router.callback_query(SetupStates.PICK_LINKED_ACCOUNT, F.data.startswith("link_pick:"))
async def on_link_pick(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    data = await state.get_data()
    platform = str(data.get("current_link_platform") or "")
    if not platform:
        await cb.message.answer("Не понял, для какой платформы подтверждать профиль. Пришли /setup еще раз.")
        return
    try:
        idx = int(str(cb.data).split(":", 1)[1])
    except Exception:
        idx = -1

    suggestions = data.get("link_suggestions") or {}
    suggestion = suggestions.get(platform) if isinstance(suggestions, dict) else None
    candidates = list((suggestion or {}).get("candidates") or [])
    if not (0 <= idx < len(candidates)):
        await cb.message.answer("Не понял выбор. Выбери вариант ниже или введи профиль вручную.")
        return

    linked_accounts = dict(data.get("linked_accounts") or {})
    linked_accounts[platform] = candidates[idx]
    queue = [item for item in (data.get("link_platform_queue") or []) if str(item) != platform]
    await state.update_data(linked_accounts=linked_accounts, link_platform_queue=queue, current_link_platform=None)
    await _ask_next_linked_account(cb.message, state)


@router.callback_query(SetupStates.PICK_LINKED_ACCOUNT, F.data == "link_manual")
async def on_link_manual(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    data = await state.get_data()
    platform = str(data.get("current_link_platform") or "")
    if not platform:
        await cb.message.answer("Не понял, для какой платформы нужен ручной ввод. Пришли /setup еще раз.")
        return
    await state.set_state(SetupStates.WAIT_LINKED_ACCOUNT_MANUAL)
    await cb.message.answer(_manual_link_prompt(platform))


@router.callback_query(SetupStates.PICK_LINKED_ACCOUNT, F.data == "link_skip")
async def on_link_skip(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    if not cb.message:
        return
    data = await state.get_data()
    platform = str(data.get("current_link_platform") or "")
    if not platform:
        await cb.message.answer("Не понял, какую платформу пропустить. Пришли /setup еще раз.")
        return
    queue = [item for item in (data.get("link_platform_queue") or []) if str(item) != platform]
    skipped = [str(item) for item in (data.get("skipped_link_platforms") or []) if str(item).strip()]
    if platform not in skipped:
        skipped.append(platform)
    await state.update_data(link_platform_queue=queue, skipped_link_platforms=skipped, current_link_platform=None)
    await _ask_next_linked_account(cb.message, state)


@router.message(SetupStates.WAIT_LINKED_ACCOUNT_MANUAL)
async def on_link_manual_input(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пришли ссылку или хендл/никнейм.")
        return
    data = await state.get_data()
    runtime = _get_setup_runtime(data)
    platform = str(data.get("current_link_platform") or "")
    if not platform:
        await message.answer("Не понял, для какой платформы подтверждать профиль. Пришли /setup еще раз.")
        return

    try:
        seed = await asyncio.to_thread(resolve_seed_for_platform, platform=platform, raw_input=raw, context=runtime)
    except SeedResolveError as exc:
        platform_state = get_platform_state(runtime, platform)
        if platform_state.state in {PLATFORM_STATE_UNAVAILABLE, PLATFORM_STATE_ERROR}:
            await message.answer(
                _platform_unavailable_message(
                    platform=platform,
                    reason=str(exc),
                    continuation="Нажми «Пропустить», и я продолжу без этой платформы.",
                )
            )
            return
        await message.answer(f"Не получилось подтвердить {_platform_label(platform)}.\nПричина: {exc}")
        return
    except Exception as exc:
        logger.warning("Manual linked account resolve failed: %s", exc)
        await message.answer(f"Не получилось подтвердить {_platform_label(platform)}.\nПричина: {exc}")
        return

    if not seed:
        await message.answer(
            f"Не получилось подтвердить {_platform_label(platform)} по этому вводу.\n"
            "Пришли точную ссылку на профиль или корректный хендл."
        )
        return

    linked_accounts = dict(data.get("linked_accounts") or {})
    linked_accounts[platform] = _linked_account_to_dict(
        seed,
        source=LinkedAccountSource.MANUAL,
        signals=["manual_input"],
    )
    queue = [item for item in (data.get("link_platform_queue") or []) if str(item) != platform]
    await state.update_data(linked_accounts=linked_accounts, link_platform_queue=queue, current_link_platform=None)
    await _ask_next_linked_account(message, state)


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
    existing_ids = {_candidate_key(d) for d in existing}

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    max_manual = 20
    added = 0
    skipped = 0
    errors: list[str] = []

    for ln in lines:
        if len(existing) >= max_manual:
            break
        try:
            s = await asyncio.to_thread(resolve_exact_seed, ln)
        except SeedResolveError as e:
            errors.append(f"{ln}: {e}")
            continue
        except Exception as e:
            errors.append(f"{ln}: {e}")
            continue
        if not s:
            errors.append(f"{ln}: профиль не подтвержден провайдером")
            continue
        key = _candidate_key(s)
        if key in existing_ids:
            skipped += 1
            continue
        existing_ids.add(key)
        existing.append(_seed_to_dict(s))
        added += 1

    await state.update_data(competitor_seeds=existing)

    parts = []
    if added:
        parts.append(f"Запомнил: {added}.")
    if skipped:
        parts.append(f"Повторы: {skipped}.")
    if errors:
        parts.append("Ошибки:")
        parts.extend(errors[:5])
    if not parts:
        parts.append("Не нашел валидных ссылок.")

    parts.append("Можно прислать еще, или нажми «Дальше».")
    kb = kb_competitors_next_or_ignore() if existing else kb_competitors_next()
    await message.answer("\n".join(parts), reply_markup=kb)


async def _start_keywords_step(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    runtime = _get_setup_runtime(data)
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    sp_id = data.get("seed_profile_id")
    sp = await db_call(SeedProfile.objects.get, id=sp_id) if sp_id else None

    seed_dict = data.get("seed") or None
    seed = _seed_from_dict(seed_dict) if isinstance(seed_dict, dict) else None
    linked_accounts = await _load_confirmed_linked_accounts(user=user, state=state)

    comp_dicts = data.get("competitor_seeds") or []
    comp_seeds: list[SeedResolution] = []
    for d in comp_dicts:
        if isinstance(d, dict):
            try:
                comp_seeds.append(_seed_from_dict(d))
            except Exception:
                continue

    context_accounts = [account for account in linked_accounts if account.external_id]
    context_seed = context_accounts[0] if context_accounts else (seed if (seed and seed.external_id) else None)
    if context_seed is None and comp_seeds:
        for idx, candidate in enumerate(comp_seeds):
            if candidate.external_id:
                context_seed = candidate
                comp_seeds = comp_seeds[:idx] + comp_seeds[idx + 1 :]
                break

    if context_seed is None:
        await state.update_data(niche_keywords=[], excluded_keywords=[])
        await state.set_state(SetupStates.ADD_NICHE)
        unavailable_lines = []
        for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM):
            platform_state = get_platform_state(runtime, platform)
            if platform_state.state in {PLATFORM_STATE_UNAVAILABLE, PLATFORM_STATE_ERROR}:
                unavailable_lines.append(
                    _platform_unavailable_message(
                        platform=platform,
                        reason=platform_state.reason or "провайдер недоступен",
                        continuation=f"Продолжаю без {_platform_label(platform)}.",
                    )
                )
        suffix = ("\n\n" + "\n".join(unavailable_lines)) if unavailable_lines else ""
        await message.answer("Чтобы подобрать конкурентов, напиши ключевые слова по нише (через запятую или с новой строки)." + suffix)
        return

    await message.answer("Секунду, подбираю ключевые слова по нише…")

    try:
        kws, source = await asyncio.to_thread(
            infer_niche_keywords,
            seed=context_seed,
            competitors=comp_seeds,
            linked_accounts=context_accounts,
            context=runtime,
        )
    except Exception as e:
        logger.warning("infer_niche_keywords failed: %s", e)
        await state.update_data(niche_keywords=[], excluded_keywords=[])
        await state.set_state(SetupStates.ADD_NICHE)
        await message.answer(
            "Не получилось проанализировать профиль автоматически.\n"
            f"Причина: {e}\n\n"
            "Пришли ключевые слова по нише (через запятую или с новой строки)."
        )
        return

    kws = [k.strip() for k in (kws or []) if isinstance(k, str) and k.strip()][:16]
    kws = build_search_ready_keywords(keywords=kws, max_keywords=8) or kws[:8]
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
    runtime = _get_setup_runtime(data)
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    linked_accounts = await _load_confirmed_linked_accounts(user=user, state=state)

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

    await message.answer("Подбираю конкурентов по платформам…")

    discovery_notes: list[str] = []
    try:
        discovery = await asyncio.to_thread(
            discover_competitors_for_onboarding,
            keywords=keywords,
            seed=seed,
            competitors=comp_seeds,
            linked_accounts=linked_accounts,
            max_youtube_search_calls=int(getattr(settings, "YT_MAX_SEARCH_CALLS_PER_SETUP", 3)),
            max_candidates_per_platform=int(getattr(settings, "MAX_COMPETITORS_PER_PLATFORM", 20)),
            context=runtime,
        )
    except PlatformOnboardingError as e:
        discovery = None
        discovery_notes = [f"Автоподбор недоступен: {e}"]
    except Exception as e:
        logger.warning("Discovery failed: %s", e)
        discovery = None
        discovery_notes = [f"Автоподбор не сработал: {e}"]

    auto_candidates = discovery.candidates if discovery else []
    if discovery:
        discovery_notes = discovery.notes
    candidates_by_id: dict[str, dict] = {}

    def add_candidate(d: dict, *, prefer: bool) -> None:
        cid = str(d.get("external_id") or "")
        platform = str(d.get("platform") or "")
        if not platform or not cid:
            return
        key = f"{platform}:{cid}"
        if seed and platform == seed.platform and cid == seed.external_id:
            return
        if key not in candidates_by_id or prefer:
            candidates_by_id[key] = d

    for s in comp_seeds:
        add_candidate(
            {
                "platform": s.platform,
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
                "platform": c.platform,
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
        details = "\n".join(discovery_notes)
        await message.answer(
            "Результат автоподбора по платформам:"
            + (f"\n{details}" if details else "")
        )
        await _ask_timezone_method(message, state)
        return

    await state.update_data(
        candidates=candidates,
        excluded_candidate_ids=[],
        prune_page=0,
        discovery_notes=discovery_notes,
    )
    await state.set_state(SetupStates.PRUNE_COMPETITORS)

    limit = int(getattr(settings, "MAX_COMPETITORS_PER_PLATFORM", 20))
    excluded: set[int] = set()
    competitor_rows = [(i, _candidate_display_name(c)) for i, c in enumerate(candidates)]
    selected_total = len(candidates) - len(excluded)
    selected_by_platform = _selected_counts_by_platform(candidates=candidates, excluded=excluded)
    text = _build_prune_text(
        selected_total=selected_total,
        selected_by_platform=selected_by_platform,
        limit=limit,
        discovery_notes=discovery_notes,
    )
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

    limit = int(getattr(settings, "MAX_COMPETITORS_PER_PLATFORM", 20))
    selected_by_platform = _selected_counts_by_platform(candidates=candidates, excluded=excluded)
    quota_error = _quota_error_text(selected_by_platform=selected_by_platform, limit=limit)
    if quota_error:
        await cb.answer(quota_error, show_alert=True)
        return

    await db_run(lambda: UserCompetitor.objects.filter(user=user).update(is_active=False))
    for i in selected_indices:
        c = candidates[i]
        meta = c.get("meta") if isinstance(c.get("meta"), dict) else {}
        await db_call(
            upsert_competitor,
            user=user,
            platform=str(c.get("platform") or ""),
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
    discovery_notes = [str(item) for item in (data.get("discovery_notes") or []) if str(item).strip()]

    limit = int(getattr(settings, "MAX_COMPETITORS_PER_PLATFORM", 20))
    competitor_rows = [(i, _candidate_display_name(c)) for i, c in enumerate(candidates)]
    selected_total = len(candidates) - len(excluded)
    selected_by_platform = _selected_counts_by_platform(candidates=candidates, excluded=excluded)
    text = _build_prune_text(
        selected_total=selected_total,
        selected_by_platform=selected_by_platform,
        limit=limit,
        discovery_notes=discovery_notes,
    )
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

    # Preserve last_run_at when user reconfigures schedule, so deltas keep working.
    now_utc = timezone.now()

    def _upsert_schedule() -> None:
        sched, created = Schedule.objects.get_or_create(
            user=user,
            defaults={
                "is_enabled": True,
                "times": times,
                "next_run_at": next_run_at,
            },
        )
        if not created:
            Schedule.objects.filter(id=sched.id).update(
                is_enabled=True,
                times=times,
                next_run_at=next_run_at,
                updated_at=now_utc,
            )

    await db_run(_upsert_schedule)

    counts = await db_run(
        lambda: {
            platform: UserCompetitor.objects.filter(user=user, is_active=True, competitor__platform=platform).count()
            for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM)
        }
    )
    await _clear_setup_runtime(state)
    await state.clear()

    await message.answer(
        "Готово.\n\n"
        + "\n".join(_build_active_competitor_summary(counts=counts))
        + "\n"
        f"Расписание: {', '.join(times)}\n"
        f"Время: {tz_label}\n"
        f"Следующий отчет: {format_dt_local(next_run_at, user.timezone_str)}\n\n"
        "Сейчас соберу первый отчет, чтобы все проверить.",
    )

    run_user_report_now.delay(user.id, trigger="setup")
