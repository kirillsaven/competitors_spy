from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from django.conf import settings

from botapp.callback_safety import (
    CallbackAck,
    MessageEdit,
    callback_started,
    log_callback_observability,
    safe_callback_ack,
    safe_edit_message,
)
from botapp.db import db_call, db_run
from botapp.keyboards import GLOBAL_BACK_CALLBACK, kb_manage_competitors
from botapp.state import CompetitorManagementStates
from botapp.user_sync import upsert_tg_user
from tracking.adapters.base import SeedResolution
from tracking.models import (
    AddedBy,
    Platform,
    Report,
    Schedule,
    SeedProfile,
    SeedStatus,
    TgUser,
    UserCompetitor,
    UserLinkedAccount,
)
from tracking.services.competitor_service import (
    PLATFORM_LABELS,
    PLATFORM_ORDER,
    deactivate_user_competitors,
    get_active_user_competitor_counts,
    list_active_user_competitor_links,
    list_active_user_competitor_links_grouped,
    upsert_competitor,
)
from tracking.services.platform_onboarding import discover_competitors_for_onboarding
from tracking.services.platform_onboarding import youtube_profile_recent_shorts_gate_status
from tracking.services.seed_resolver import (
    candidate_platforms_for_exact_seed,
    resolve_exact_seed,
    resolve_instagram_seeds_batch,
)
from tracking.services.suggested_competitors import (
    activate_instagram_suggested_competitor,
    activate_youtube_suggested_competitor,
    record_instagram_suggested_competitor_acceptance,
    record_youtube_suggested_competitor_acceptance,
)
from tracking.services.setup_runtime import SetupRunContext

router = Router()
logger = logging.getLogger(__name__)

_PAGE_SIZE = 8


def _discovery_target_per_platform() -> int:
    return int(
        getattr(
            settings,
            "DISCOVERY_TARGET_COMPETITORS_PER_PLATFORM",
            getattr(settings, "MAX_COMPETITORS_PER_PLATFORM", 20),
        )
        or 20
    )


def _format_competitor_name(link: UserCompetitor) -> str:
    competitor = link.competitor
    name = competitor.display_name or competitor.handle or competitor.external_id
    handle = str(competitor.handle or "").strip().lstrip("@")
    if handle:
        return f"{name} (@{handle})"
    return name


def _seed_meta(seed: SeedResolution) -> dict[str, str] | None:
    meta: dict[str, str] = {}
    if seed.description:
        meta["description"] = seed.description
    if seed.uploads_playlist_id:
        meta["uploads_playlist_id"] = seed.uploads_playlist_id
    return meta or None


def _counts_lines(counts: dict[str, int]) -> list[str]:
    return [f"{PLATFORM_LABELS[platform]}: {counts.get(platform, 0)}" for platform in PLATFORM_ORDER]


def _is_setup_complete(*, user: TgUser) -> bool:
    return Schedule.objects.filter(user=user).exists()


def _competitor_display_name(candidate: dict) -> str:
    platform = str(candidate.get("platform") or "").strip()
    label = {
        Platform.YOUTUBE: "YT",
        Platform.TIKTOK: "TT",
        Platform.INSTAGRAM: "IG",
    }.get(platform, platform.upper() or "?")
    name = str(candidate.get("display_name") or candidate.get("handle") or candidate.get("external_id") or "").strip()
    handle = str(candidate.get("handle") or "").strip().lstrip("@")
    if handle:
        return f"[{label}] {name} (@{handle})"
    return f"[{label}] {name}"


def _active_link_rows(*, user: TgUser) -> list[dict]:
    rows: list[dict] = []
    for link in list_active_user_competitor_links(user=user):
        rows.append(
            {
                "competitor_id": int(link.competitor_id),
                "platform": str(link.competitor.platform or ""),
                "display_name": _format_competitor_name(link),
                "url": str(link.competitor.url or "").strip() or None,
            }
        )
    return rows


def _seed_resolution_from_linked_account(account: UserLinkedAccount) -> SeedResolution | None:
    external_id = str(account.external_id or "").strip()
    if not external_id:
        return None
    meta = account.meta if isinstance(account.meta, dict) else {}
    return SeedResolution(
        platform=account.platform,
        external_id=external_id,
        handle=str(account.handle or "").strip() or None,
        url=str(account.url or "").strip(),
        title=str(account.display_name or account.handle or external_id).strip(),
        description=str(meta.get("description") or "").strip() or None,
        uploads_playlist_id=str(meta.get("uploads_playlist_id") or "").strip() or None,
    )


def _seed_resolution_from_user_link(link: UserCompetitor) -> SeedResolution | None:
    competitor = link.competitor
    external_id = str(competitor.external_id or "").strip()
    if not external_id:
        return None
    meta = competitor.meta if isinstance(competitor.meta, dict) else {}
    return SeedResolution(
        platform=competitor.platform,
        external_id=external_id,
        handle=str(competitor.handle or "").strip() or None,
        url=str(competitor.url or "").strip(),
        title=str(competitor.display_name or competitor.handle or external_id).strip(),
        description=str(meta.get("description") or "").strip() or None,
        uploads_playlist_id=str(meta.get("uploads_playlist_id") or "").strip() or None,
    )


def _load_add_candidates_for_user(*, user: TgUser) -> tuple[list[dict], list[str]]:
    seed_profile = (
        SeedProfile.objects.filter(user=user, status=SeedStatus.RESOLVED)
        .order_by("-id")
        .first()
    )
    if seed_profile is None:
        raise RuntimeError("Не нашел сохраненный setup. Сначала заново заверши /setup.")
    seed_input = str(seed_profile.canonical_url or seed_profile.raw_input or "").strip()
    if not seed_input:
        raise RuntimeError("В setup не сохранился исходный профиль. Запусти /setup заново.")
    keywords = [str(item).strip() for item in (seed_profile.niche_keywords or []) if str(item).strip()]
    if not keywords:
        raise RuntimeError("Не нашел ключевые слова ниши. Запусти /setup заново.")

    context = SetupRunContext()
    seed = resolve_exact_seed(seed_input, context=context)
    if seed is None:
        raise RuntimeError("Не смог заново подтвердить seed-профиль для подбора конкурентов.")

    linked_accounts = [
        seed_resolution
        for seed_resolution in (
            _seed_resolution_from_linked_account(account)
            for account in UserLinkedAccount.objects.filter(user=user).order_by("id")
        )
        if seed_resolution is not None
    ]
    active_competitors = [
        seed_resolution
        for seed_resolution in (
            _seed_resolution_from_user_link(link)
            for link in list_active_user_competitor_links(user=user)
        )
        if seed_resolution is not None
    ]
    active_keys = {(item.platform, item.external_id) for item in active_competitors}

    outcome = discover_competitors_for_onboarding(
        keywords=keywords,
        seed=seed,
        competitors=active_competitors,
        linked_accounts=linked_accounts,
        max_youtube_search_calls=int(getattr(settings, "YT_MAX_SEARCH_CALLS_PER_SETUP", 5) or 5),
        max_candidates_per_platform=_discovery_target_per_platform(),
        context=context,
    )
    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for candidate in outcome.candidates:
        key = (candidate.platform, candidate.external_id)
        if key in active_keys or key in seen:
            continue
        seen.add(key)
        candidates.append(
            {
                "platform": candidate.platform,
                "external_id": candidate.external_id,
                "handle": candidate.handle,
                "url": candidate.url,
                "display_name": candidate.display_name,
                "added_by": AddedBy.AUTO,
                "meta": {},
            }
        )
    return candidates, list(outcome.notes or [])


def _build_picker_text(
    *,
    action_label: str,
    selected_total: int,
    counts: dict[str, int],
    notes: list[str] | None = None,
) -> str:
    lines = [
        action_label,
        f"Выбрано: {selected_total}",
        *_counts_lines(counts),
    ]
    extra = [str(item).strip() for item in (notes or []) if str(item).strip()]
    if extra:
        lines.append("")
        lines.extend(extra[:5])
    lines.append("Когда готово, нажми «Готово».")
    return "\n".join(lines)


def _picker_page_count(total: int, page_size: int) -> int:
    return max(1, (max(0, total) + page_size - 1) // page_size)


def _clamp_picker_page(page: int, *, total: int, page_size: int) -> int:
    return min(max(0, page), _picker_page_count(total, page_size) - 1)


def _make_add_picker_rows(candidates: list[dict]) -> list[dict]:
    return [
        {
            "id": idx,
            "name": _competitor_display_name(candidate),
            "url": str(candidate.get("url") or "").strip() or None,
        }
        for idx, candidate in enumerate(candidates)
    ]


def _make_add_platform_by_id(candidates: list[dict]) -> dict[str, str]:
    return {str(idx): str(candidate.get("platform") or "") for idx, candidate in enumerate(candidates)}


def _make_remove_picker_rows(rows: list[dict]) -> list[dict]:
    return [
        {
            "id": int(row.get("competitor_id") or 0),
            "name": str(row.get("display_name") or "").strip(),
            "url": str(row.get("url") or "").strip() or None,
        }
        for row in rows
        if int(row.get("competitor_id") or 0) > 0
    ]


def _make_remove_platform_by_id(rows: list[dict]) -> dict[str, str]:
    return {
        str(int(row.get("competitor_id") or 0)): str(row.get("platform") or "")
        for row in rows
        if int(row.get("competitor_id") or 0) > 0
    }


def _picker_rows_from_state(rows: list[dict] | None) -> list[tuple[int, str, str | None]]:
    out: list[tuple[int, str, str | None]] = []
    for row in rows or []:
        try:
            if isinstance(row, dict):
                row_id = int(row.get("id") or 0)
                name = str(row.get("name") or "").strip()
                url = str(row.get("url") or "").strip() or None
            else:
                row_id = int(row[0])
                name = str(row[1] or "").strip()
                url = str(row[2] or "").strip() or None
        except Exception:
            continue
        out.append((row_id, name, url))
    return out


def _selected_platform_counts(*, selected_ids: set[int], platform_by_id: dict) -> dict[str, int]:
    counts = {platform: 0 for platform in PLATFORM_ORDER}
    for item_id in selected_ids:
        platform = str(platform_by_id.get(str(item_id)) or platform_by_id.get(item_id) or "")
        if platform:
            counts[platform] = counts.get(platform, 0) + 1
    return counts


def _log_picker_callback(
    *,
    ack: CallbackAck,
    edit: MessageEdit | None = None,
    page_before: int,
    page_after: int,
    candidate_count: int,
    keyboard_render_ms: float,
    status: str,
    error: str | None = None,
) -> None:
    log_callback_observability(
        logger,
        event_name="competitor_picker_callback_observability",
        ack=ack,
        edit=edit,
        failure_reason=error,
        page_before=page_before,
        page_after=page_after,
        candidate_count=candidate_count,
        keyboard_render_ms=f"{keyboard_render_ms:.1f}",
        status=status,
    )


def _manual_add_prompt_text() -> str:
    return (
        "Отправь ссылки или хэндлы конкурентов, по одному на строку.\n"
        "Поддерживаются YouTube, TikTok и Instagram.\n"
        "Для YouTube добавлю только канал, который проходит правило: минимум 2 Shorts за последние 60 дней."
    )


def _manual_add_inputs(raw_text: str) -> list[str]:
    return [line.strip() for line in str(raw_text or "").splitlines() if line.strip()]


def _youtube_suggestion_added_text(*, display_name: str, counts: dict[str, int]) -> str:
    return (
        f"Добавил конкурента в YouTube: {display_name}.\n"
        f"Активных YouTube-конкурентов: {counts.get(Platform.YOUTUBE, 0)}"
    )


def _instagram_suggestion_added_text(*, display_name: str, counts: dict[str, int]) -> str:
    return (
        f"Добавил конкурента в Instagram: {display_name}.\n"
        f"Активных Instagram-конкурентов: {counts.get(Platform.INSTAGRAM, 0)}"
    )


async def _render_add_picker(
    message: Message,
    state: FSMContext,
    *,
    data: dict | None = None,
    edit_mode: str = "text",
    callback_info: dict | None = None,
) -> None:
    data = data if data is not None else await state.get_data()
    candidates = list(data.get("competitor_add_candidates") or [])
    selected_ids = {int(item) for item in (data.get("competitor_add_selected_ids") or [])}
    page = _clamp_picker_page(int(data.get("competitor_add_page") or 0), total=len(candidates), page_size=_PAGE_SIZE)
    notes = [str(item) for item in (data.get("competitor_add_notes") or []) if str(item).strip()]
    rows = _picker_rows_from_state(data.get("competitor_add_picker_rows"))
    if not rows:
        rows = _picker_rows_from_state(_make_add_picker_rows(candidates))
    platform_by_id = dict(data.get("competitor_add_platform_by_id") or {})
    if not platform_by_id:
        platform_by_id = _make_add_platform_by_id(candidates)
    counts = _selected_platform_counts(selected_ids=selected_ids, platform_by_id=platform_by_id)
    text = _build_picker_text(
        action_label="Нашел кандидатов для добавления. Выбирай профили кнопками ниже.",
        selected_total=len(selected_ids),
        counts=counts,
        notes=notes,
    )
    keyboard_started = callback_started()
    kb = kb_manage_competitors(
        competitor_rows=rows,
        selected_ids=selected_ids,
        page=page,
        page_size=_PAGE_SIZE,
        toggle_prefix="compadd_toggle",
        page_prefix="compadd_page",
        all_callback="compadd_all",
        done_callback="compadd_done",
        done_text="Добавить",
    )
    keyboard_render_ms = (callback_started() - keyboard_started) * 1000
    edit = await safe_edit_message(
        message,
        text=text,
        reply_markup=kb,
        edit_mode=edit_mode,
    )
    if edit.failure_reason:
        logger.warning("competitor_add_picker_render_failed edit_path=%s error=%s", edit.path, edit.failure_reason)
    if callback_info:
        ack = callback_info["ack"]
        _log_picker_callback(
            ack=ack,
            edit=edit,
            page_before=int(callback_info.get("page_before") or 0),
            page_after=page,
            candidate_count=len(rows),
            keyboard_render_ms=keyboard_render_ms,
            status="failure" if edit.failure_reason else "success",
        )


async def _render_remove_picker(
    message: Message,
    state: FSMContext,
    *,
    data: dict | None = None,
    edit_mode: str = "text",
    callback_info: dict | None = None,
) -> None:
    data = data if data is not None else await state.get_data()
    rows = list(data.get("competitor_remove_rows") or [])
    selected_ids = {int(item) for item in (data.get("competitor_remove_selected_ids") or [])}
    kb_rows = _picker_rows_from_state(data.get("competitor_remove_picker_rows"))
    if not kb_rows:
        kb_rows = _picker_rows_from_state(_make_remove_picker_rows(rows))
    page = _clamp_picker_page(int(data.get("competitor_remove_page") or 0), total=len(kb_rows), page_size=_PAGE_SIZE)
    platform_by_id = dict(data.get("competitor_remove_platform_by_id") or {})
    if not platform_by_id:
        platform_by_id = _make_remove_platform_by_id(rows)
    counts = _selected_platform_counts(selected_ids=selected_ids, platform_by_id=platform_by_id)
    text = _build_picker_text(
        action_label="Выбери конкурентов, которых нужно убрать из активного списка.",
        selected_total=len(selected_ids),
        counts=counts,
    )
    keyboard_started = callback_started()
    kb = kb_manage_competitors(
        competitor_rows=kb_rows,
        selected_ids=selected_ids,
        page=page,
        page_size=_PAGE_SIZE,
        toggle_prefix="comprem_toggle",
        page_prefix="comprem_page",
        all_callback="comprem_all",
        done_callback="comprem_done",
        done_text="Убрать",
    )
    keyboard_render_ms = (callback_started() - keyboard_started) * 1000
    edit = await safe_edit_message(
        message,
        text=text,
        reply_markup=kb,
        edit_mode=edit_mode,
    )
    if edit.failure_reason:
        logger.warning("competitor_remove_picker_render_failed edit_path=%s error=%s", edit.path, edit.failure_reason)
    if callback_info:
        ack = callback_info["ack"]
        _log_picker_callback(
            ack=ack,
            edit=edit,
            page_before=int(callback_info.get("page_before") or 0),
            page_after=page,
            candidate_count=len(kb_rows),
            keyboard_render_ms=keyboard_render_ms,
            status="failure" if edit.failure_reason else "success",
        )


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
    setup_complete = await db_run(lambda: _is_setup_complete(user=user))
    if not setup_complete:
        await message.answer("Сначала заверши /setup.")
        return

    grouped = await db_run(lambda: list_active_user_competitor_links_grouped(user=user))
    lines = ["Активные конкуренты:"]
    for platform in PLATFORM_ORDER:
        links = grouped.get(platform, [])
        lines.append(f"{PLATFORM_LABELS[platform]} ({len(links)}):")
        if not links:
            lines.append("—")
            continue
        for index, link in enumerate(links, start=1):
            lines.append(f"{index}. {_format_competitor_name(link)}")
    lines.append("")
    lines.append("/competitors_add - добавить вручную по ссылке или хэндлу")
    lines.append("/competitors_suggest - выбрать из списка и добавить")
    lines.append("/competitors_remove - выбрать из списка и убрать")
    await message.answer("\n".join(lines))


@router.message(Command("competitors_suggest"))
async def cmd_competitors_suggest(message: Message, state: FSMContext) -> None:
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
    setup_complete = await db_run(lambda: _is_setup_complete(user=user))
    if not setup_complete:
        await message.answer("Сначала заверши /setup.")
        return

    loading_message = await message.answer("Ищу кандидатов для добавления...")
    try:
        candidates, notes = await asyncio.to_thread(_load_add_candidates_for_user, user=user)
    except Exception as exc:
        await state.clear()
        await loading_message.edit_text(f"Не смог подготовить список кандидатов: {exc}")
        return
    if not candidates:
        await state.clear()
        details = "\n".join(notes[:5])
        text = "Не нашел новых кандидатов для добавления."
        if details:
            text = f"{text}\n\n{details}"
        await loading_message.edit_text(text)
        return

    await state.update_data(
        user_id=user.id,
        competitor_add_candidates=candidates,
        competitor_add_picker_rows=_make_add_picker_rows(candidates),
        competitor_add_platform_by_id=_make_add_platform_by_id(candidates),
        competitor_add_selected_ids=[],
        competitor_add_page=0,
        competitor_add_notes=notes,
    )
    await state.set_state(CompetitorManagementStates.PICK_COMPETITORS_ADD)
    await _render_add_picker(loading_message, state)


@router.message(Command("competitors_add"))
@router.message(Command("competitor_add"))
async def cmd_competitors_add(message: Message, state: FSMContext) -> None:
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
    setup_complete = await db_run(lambda: _is_setup_complete(user=user))
    if not setup_complete:
        await message.answer("Сначала заверши /setup.")
        return
    await state.clear()
    await state.update_data(user_id=user.id)
    await state.set_state(CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT)
    await message.answer(_manual_add_prompt_text())


cmd_competitor_add_manual = cmd_competitors_add


@router.message(Command("competitors_remove"))
async def cmd_competitors_remove(message: Message, state: FSMContext) -> None:
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
    setup_complete = await db_run(lambda: _is_setup_complete(user=user))
    if not setup_complete:
        await message.answer("Сначала заверши /setup.")
        return

    rows = await db_run(lambda: _active_link_rows(user=user))
    if not rows:
        await state.clear()
        await message.answer("Активных конкурентов для удаления сейчас нет.")
        return

    picker = await message.answer("Готовлю список активных конкурентов...")
    await state.update_data(
        user_id=user.id,
        competitor_remove_rows=rows,
        competitor_remove_picker_rows=_make_remove_picker_rows(rows),
        competitor_remove_platform_by_id=_make_remove_platform_by_id(rows),
        competitor_remove_selected_ids=[],
        competitor_remove_page=0,
    )
    await state.set_state(CompetitorManagementStates.PICK_COMPETITORS_REMOVE)
    await _render_remove_picker(picker, state)


@router.callback_query(F.data.startswith("suggytadd:"))
async def on_suggested_youtube_add(cb: CallbackQuery) -> None:
    if not cb.message or not cb.from_user:
        return
    try:
        _, report_id_raw, suggestion_idx_raw = str(cb.data).split(":", 2)
        report_id = int(report_id_raw)
        suggestion_idx = int(suggestion_idx_raw)
    except Exception:
        await cb.answer("Не понял, какую подсказку добавить.", show_alert=True)
        return

    user, _ = await db_call(
        upsert_tg_user,
        telegram_user_id=cb.from_user.id,
        chat_id=cb.message.chat.id,
        username=cb.from_user.username,
        first_name=cb.from_user.first_name,
        last_name=cb.from_user.last_name,
        language_code=cb.from_user.language_code,
    )
    report = await db_run(lambda: Report.objects.filter(id=report_id, user=user).first())
    if report is None:
        await cb.answer("Подсказка устарела. Дождись нового отчета.", show_alert=True)
        return

    youtube_suggestions = ((((report.payload or {}).get("suggested_competitors") or {}).get("youtube")) or {})
    delivery = dict(youtube_suggestions.get("delivery") or {})
    delivered_items = [item for item in list(delivery.get("sent_items") or []) if isinstance(item, dict)]
    items = delivered_items or [item for item in list(youtube_suggestions.get("items") or []) if isinstance(item, dict)]
    if suggestion_idx < 0 or suggestion_idx >= len(items):
        await cb.answer("Не нашел эту подсказку в отчете.", show_alert=True)
        return

    suggestion = items[suggestion_idx]
    try:
        result = await db_call(
            activate_youtube_suggested_competitor,
            user=user,
            suggestion=suggestion,
            added_by=AddedBy.SUGGESTED,
        )
    except Exception as exc:
        acceptance = await db_call(
            record_youtube_suggested_competitor_acceptance,
            report=report,
            suggestion=suggestion,
            status="error",
        )
        logger.info(
            "suggested_competitor_click_observability user_id=%s report_id=%s channel_id=%s status=%s "
            "clicked_add=%s added=%s already_active=%s suggestion_source=%s suggestion_reason=%s",
            user.id,
            report.id,
            str(suggestion.get("channel_id") or "").strip(),
            "error",
            acceptance.get("clicked_add", 0),
            acceptance.get("added", 0),
            acceptance.get("already_active", 0),
            ((((report.payload or {}).get("suggested_competitors") or {}).get("youtube") or {}).get("source")),
            suggestion.get("suggestion_reason"),
        )
        await cb.answer(str(exc), show_alert=True)
        return

    channel_id = str(suggestion.get("channel_id") or "").strip()
    acceptance = await db_call(
        record_youtube_suggested_competitor_acceptance,
        report=report,
        suggestion=suggestion,
        status=result.status,
    )
    logger.info(
        "suggested_competitor_click_observability user_id=%s report_id=%s channel_id=%s status=%s "
        "clicked_add=%s added=%s already_active=%s suggestion_source=%s suggestion_reason=%s",
        user.id,
        report.id,
        channel_id,
        result.status,
        acceptance.get("clicked_add", 0),
        acceptance.get("added", 0),
        acceptance.get("already_active", 0),
        ((((report.payload or {}).get("suggested_competitors") or {}).get("youtube") or {}).get("source")),
        suggestion.get("suggestion_reason"),
    )
    if result.status == "already_active":
        await cb.message.answer(f"{result.display_name} уже есть в активных YouTube-конкурентах.")
        await cb.answer("Уже в активном списке.")
        return

    await cb.message.answer(_youtube_suggestion_added_text(display_name=result.display_name, counts=result.counts))
    await cb.answer("Конкурент добавлен.")
    logger.info(
        "suggested_competitor_added_via_click user_id=%s report_id=%s channel_id=%s status=%s",
        user.id,
        report.id,
        channel_id,
        result.status,
    )


@router.callback_query(F.data.startswith("sugigadd:"))
async def on_suggested_instagram_add(cb: CallbackQuery) -> None:
    if not cb.message or not cb.from_user:
        return
    try:
        _, report_id_raw, suggestion_idx_raw = str(cb.data).split(":", 2)
        report_id = int(report_id_raw)
        suggestion_idx = int(suggestion_idx_raw)
    except Exception:
        await cb.answer("Не понял, какую подсказку добавить.", show_alert=True)
        return

    user, _ = await db_call(
        upsert_tg_user,
        telegram_user_id=cb.from_user.id,
        chat_id=cb.message.chat.id,
        username=cb.from_user.username,
        first_name=cb.from_user.first_name,
        last_name=cb.from_user.last_name,
        language_code=cb.from_user.language_code,
    )
    report = await db_run(lambda: Report.objects.filter(id=report_id, user=user).first())
    if report is None:
        await cb.answer("Подсказка устарела. Дождись нового отчета.", show_alert=True)
        return

    instagram_suggestions = ((((report.payload or {}).get("suggested_competitors") or {}).get("instagram")) or {})
    delivery = dict(instagram_suggestions.get("delivery") or {})
    delivered_items = [item for item in list(delivery.get("sent_items") or []) if isinstance(item, dict)]
    items = delivered_items or [item for item in list(instagram_suggestions.get("items") or []) if isinstance(item, dict)]
    if suggestion_idx < 0 or suggestion_idx >= len(items):
        await cb.answer("Не нашел эту подсказку в отчете.", show_alert=True)
        return

    suggestion = items[suggestion_idx]
    try:
        result = await db_call(
            activate_instagram_suggested_competitor,
            user=user,
            suggestion=suggestion,
            added_by=AddedBy.SUGGESTED,
        )
    except Exception as exc:
        acceptance = await db_call(
            record_instagram_suggested_competitor_acceptance,
            report=report,
            suggestion=suggestion,
            status="error",
        )
        logger.info(
            "suggested_competitor_click_observability user_id=%s report_id=%s competitor_external_id=%s status=%s "
            "clicked_add=%s added=%s already_active=%s suggestion_source=%s suggestion_reason=%s",
            user.id,
            report.id,
            str(suggestion.get("competitor_external_id") or "").strip(),
            "error",
            acceptance.get("clicked_add", 0),
            acceptance.get("added", 0),
            acceptance.get("already_active", 0),
            ((((report.payload or {}).get("suggested_competitors") or {}).get("instagram")) or {}).get("source"),
            suggestion.get("suggestion_reason"),
        )
        await cb.answer(str(exc), show_alert=True)
        return

    competitor_external_id = str(suggestion.get("competitor_external_id") or "").strip()
    acceptance = await db_call(
        record_instagram_suggested_competitor_acceptance,
        report=report,
        suggestion=suggestion,
        status=result.status,
    )
    logger.info(
        "suggested_competitor_click_observability user_id=%s report_id=%s competitor_external_id=%s status=%s "
        "clicked_add=%s added=%s already_active=%s suggestion_source=%s suggestion_reason=%s",
        user.id,
        report.id,
        competitor_external_id,
        result.status,
        acceptance.get("clicked_add", 0),
        acceptance.get("added", 0),
        acceptance.get("already_active", 0),
        ((((report.payload or {}).get("suggested_competitors") or {}).get("instagram")) or {}).get("source"),
        suggestion.get("suggestion_reason"),
    )
    if result.status == "already_active":
        await cb.message.answer(f"{result.display_name} уже есть в активных Instagram-конкурентах.")
        await cb.answer("Уже в активном списке.")
        return

    await cb.message.answer(_instagram_suggestion_added_text(display_name=result.display_name, counts=result.counts))
    await cb.answer("Конкурент добавлен.")
    logger.info(
        "suggested_competitor_added_via_click user_id=%s report_id=%s competitor_external_id=%s status=%s",
        user.id,
        report.id,
        competitor_external_id,
        result.status,
    )


@router.callback_query(CompetitorManagementStates.PICK_COMPETITORS_ADD, F.data == "noop")
@router.callback_query(CompetitorManagementStates.PICK_COMPETITORS_REMOVE, F.data == "noop")
async def on_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(
    CompetitorManagementStates.PICK_COMPETITORS_ADD,
    F.data == GLOBAL_BACK_CALLBACK,
)
@router.callback_query(
    CompetitorManagementStates.PICK_COMPETITORS_REMOVE,
    F.data == GLOBAL_BACK_CALLBACK,
)
async def on_back(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.clear()
    if cb.message:
        await cb.message.answer("Ок, ничего не менял.")


@router.callback_query(CompetitorManagementStates.PICK_COMPETITORS_ADD, F.data.startswith("compadd_page:"))
async def on_add_page(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    ack = await safe_callback_ack(cb, callback_type="compadd_page", logger=logger, started_at=started_at)
    if not cb.message:
        return
    try:
        requested_page = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    data = await state.get_data()
    candidates = list(data.get("competitor_add_candidates") or [])
    page_before = int(data.get("competitor_add_page") or 0)
    page_after = _clamp_picker_page(requested_page, total=len(candidates), page_size=_PAGE_SIZE)
    await state.update_data(competitor_add_page=page_after)
    data["competitor_add_page"] = page_after
    await _render_add_picker(
        cb.message,
        state,
        data=data,
        edit_mode="markup",
        callback_info={
            "ack": ack,
            "page_before": page_before,
        },
    )


@router.callback_query(CompetitorManagementStates.PICK_COMPETITORS_ADD, F.data.startswith("compadd_toggle:"))
async def on_add_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    ack = await safe_callback_ack(cb, callback_type="compadd_toggle", logger=logger, started_at=started_at)
    if not cb.message:
        return
    try:
        idx = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    data = await state.get_data()
    page_before = int(data.get("competitor_add_page") or 0)
    selected = {int(item) for item in (data.get("competitor_add_selected_ids") or [])}
    if idx in selected:
        selected.remove(idx)
    else:
        selected.add(idx)
    selected_sorted = sorted(selected)
    await state.update_data(competitor_add_selected_ids=selected_sorted)
    data["competitor_add_selected_ids"] = selected_sorted
    await _render_add_picker(
        cb.message,
        state,
        data=data,
        callback_info={
            "ack": ack,
            "page_before": page_before,
        },
    )


@router.callback_query(CompetitorManagementStates.PICK_COMPETITORS_ADD, F.data == "compadd_all")
async def on_add_all(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    ack = await safe_callback_ack(cb, callback_type="compadd_all", logger=logger, started_at=started_at)
    if not cb.message:
        return
    data = await state.get_data()
    page_before = int(data.get("competitor_add_page") or 0)
    candidates = list(data.get("competitor_add_candidates") or [])
    selected_ids = list(range(len(candidates)))
    await state.update_data(competitor_add_selected_ids=selected_ids)
    data["competitor_add_selected_ids"] = selected_ids
    await _render_add_picker(
        cb.message,
        state,
        data=data,
        callback_info={
            "ack": ack,
            "page_before": page_before,
        },
    )


@router.callback_query(CompetitorManagementStates.PICK_COMPETITORS_ADD, F.data == "compadd_done")
async def on_add_done(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    if not cb.message:
        return
    data = await state.get_data()
    page_before = int(data.get("competitor_add_page") or 0)
    selected_ids = {int(item) for item in (data.get("competitor_add_selected_ids") or [])}
    candidates = list(data.get("competitor_add_candidates") or [])
    if not selected_ids:
        ack = await safe_callback_ack(
            cb,
            callback_type="compadd_done",
            logger=logger,
            started_at=started_at,
            text="Сначала выбери хотя бы одного конкурента.",
            show_alert=True,
        )
        _log_picker_callback(
            ack=ack,
            page_before=page_before,
            page_after=page_before,
            candidate_count=len(candidates),
            keyboard_render_ms=0.0,
            status="empty_selection",
        )
        return
    ack = await safe_callback_ack(cb, callback_type="compadd_done", logger=logger, started_at=started_at)

    user = await db_call(TgUser.objects.get, id=data["user_id"])
    counts = await db_run(lambda: get_active_user_competitor_counts(user=user))
    added = 0
    skipped = 0
    errors: list[str] = []

    for idx in sorted(selected_ids):
        if idx < 0 or idx >= len(candidates):
            continue
        candidate = candidates[idx]
        platform = str(candidate.get("platform") or "")
        await db_call(
            upsert_competitor,
            user=user,
            platform=platform,
            external_id=str(candidate.get("external_id") or ""),
            handle=candidate.get("handle"),
            url=str(candidate.get("url") or ""),
            display_name=candidate.get("display_name"),
            added_by=str(candidate.get("added_by") or AddedBy.AUTO),
            meta=candidate.get("meta") if isinstance(candidate.get("meta"), dict) else None,
        )
        counts[platform] = counts.get(platform, 0) + 1
        added += 1

    skipped = len(selected_ids) - added - len(errors)
    await state.clear()
    await cb.message.answer(
        _build_add_remove_summary(
            action="Добавлено",
            changed=added,
            skipped=max(0, skipped),
            errors=errors,
            counts=counts,
        )
    )
    _log_picker_callback(
        ack=ack,
        page_before=page_before,
        page_after=page_before,
        candidate_count=len(candidates),
        keyboard_render_ms=0.0,
        status="success",
    )


@router.callback_query(CompetitorManagementStates.PICK_COMPETITORS_REMOVE, F.data.startswith("comprem_page:"))
async def on_remove_page(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    ack = await safe_callback_ack(cb, callback_type="comprem_page", logger=logger, started_at=started_at)
    if not cb.message:
        return
    try:
        requested_page = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    data = await state.get_data()
    rows = _picker_rows_from_state(data.get("competitor_remove_picker_rows"))
    if not rows:
        rows = _picker_rows_from_state(_make_remove_picker_rows(list(data.get("competitor_remove_rows") or [])))
    page_before = int(data.get("competitor_remove_page") or 0)
    page_after = _clamp_picker_page(requested_page, total=len(rows), page_size=_PAGE_SIZE)
    await state.update_data(competitor_remove_page=page_after)
    data["competitor_remove_page"] = page_after
    await _render_remove_picker(
        cb.message,
        state,
        data=data,
        edit_mode="markup",
        callback_info={
            "ack": ack,
            "page_before": page_before,
        },
    )


@router.callback_query(CompetitorManagementStates.PICK_COMPETITORS_REMOVE, F.data.startswith("comprem_toggle:"))
async def on_remove_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    ack = await safe_callback_ack(cb, callback_type="comprem_toggle", logger=logger, started_at=started_at)
    if not cb.message:
        return
    try:
        competitor_id = int(str(cb.data).split(":", 1)[1])
    except Exception:
        return
    data = await state.get_data()
    page_before = int(data.get("competitor_remove_page") or 0)
    selected = {int(item) for item in (data.get("competitor_remove_selected_ids") or [])}
    if competitor_id in selected:
        selected.remove(competitor_id)
    else:
        selected.add(competitor_id)
    selected_sorted = sorted(selected)
    await state.update_data(competitor_remove_selected_ids=selected_sorted)
    data["competitor_remove_selected_ids"] = selected_sorted
    await _render_remove_picker(
        cb.message,
        state,
        data=data,
        callback_info={
            "ack": ack,
            "page_before": page_before,
        },
    )


@router.callback_query(CompetitorManagementStates.PICK_COMPETITORS_REMOVE, F.data == "comprem_all")
async def on_remove_all(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    ack = await safe_callback_ack(cb, callback_type="comprem_all", logger=logger, started_at=started_at)
    if not cb.message:
        return
    data = await state.get_data()
    page_before = int(data.get("competitor_remove_page") or 0)
    rows = list(data.get("competitor_remove_rows") or [])
    selected = [int(row.get("competitor_id") or 0) for row in rows if int(row.get("competitor_id") or 0) > 0]
    await state.update_data(competitor_remove_selected_ids=selected)
    data["competitor_remove_selected_ids"] = selected
    await _render_remove_picker(
        cb.message,
        state,
        data=data,
        callback_info={
            "ack": ack,
            "page_before": page_before,
        },
    )


@router.callback_query(CompetitorManagementStates.PICK_COMPETITORS_REMOVE, F.data == "comprem_done")
async def on_remove_done(cb: CallbackQuery, state: FSMContext) -> None:
    started_at = callback_started()
    if not cb.message:
        return
    data = await state.get_data()
    page_before = int(data.get("competitor_remove_page") or 0)
    selected_ids = {int(item) for item in (data.get("competitor_remove_selected_ids") or [])}
    if not selected_ids:
        rows = list(data.get("competitor_remove_rows") or [])
        ack = await safe_callback_ack(
            cb,
            callback_type="comprem_done",
            logger=logger,
            started_at=started_at,
            text="Сначала выбери хотя бы одного конкурента.",
            show_alert=True,
        )
        _log_picker_callback(
            ack=ack,
            page_before=page_before,
            page_after=page_before,
            candidate_count=len(rows),
            keyboard_render_ms=0.0,
            status="empty_selection",
        )
        return
    ack = await safe_callback_ack(cb, callback_type="comprem_done", logger=logger, started_at=started_at)

    user = await db_call(TgUser.objects.get, id=data["user_id"])
    removed = await db_run(lambda: deactivate_user_competitors(user=user, competitor_ids=sorted(selected_ids)))
    counts = await db_run(lambda: get_active_user_competitor_counts(user=user))
    await state.clear()
    await cb.message.answer(
        _build_add_remove_summary(
            action="Удалено",
            changed=removed,
            skipped=max(0, len(selected_ids) - removed),
            errors=[],
            counts=counts,
        )
    )
    _log_picker_callback(
        ack=ack,
        page_before=page_before,
        page_after=page_before,
        candidate_count=len(data.get("competitor_remove_rows") or []),
        keyboard_render_ms=0.0,
        status="success",
    )


@router.message(CompetitorManagementStates.PICK_COMPETITORS_ADD)
@router.message(CompetitorManagementStates.PICK_COMPETITORS_REMOVE)
async def on_picker_text(message: Message) -> None:
    await message.answer("Выбери профили кнопками ниже.")


@router.message(CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT)
async def on_competitor_add_manual_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await db_call(TgUser.objects.get, id=data["user_id"])
    raw_inputs = _manual_add_inputs(message.text or "")
    if not raw_inputs:
        await message.answer("Не увидел ни одной ссылки или хэндла. Отправь хотя бы один профиль.")
        return
    await message.answer("Проверяю профили...")

    try:
        counts = await db_run(lambda: get_active_user_competitor_counts(user=user))
        added = 0
        errors: list[str] = []
        context = SetupRunContext()

        resolved: list[tuple[str, SeedResolution | None]] = [(raw_input, None) for raw_input in raw_inputs]
        failed_resolution_indexes: set[int] = set()
        instagram_batch_indexes: list[int] = []
        instagram_batch_inputs: list[str] = []
        for idx, raw_input in enumerate(raw_inputs):
            if candidate_platforms_for_exact_seed(raw_input) == [Platform.INSTAGRAM]:
                instagram_batch_indexes.append(idx)
                instagram_batch_inputs.append(raw_input)
        instagram_batch_index_set = set(instagram_batch_indexes)

        if instagram_batch_inputs:
            try:
                instagram_seeds = await asyncio.to_thread(
                    resolve_instagram_seeds_batch,
                    instagram_batch_inputs,
                    context=context,
                )
            except Exception as exc:
                for idx in instagram_batch_indexes:
                    raw_input = raw_inputs[idx]
                    errors.append(f"{raw_input}: не смог подтвердить профиль ({exc})")
                    failed_resolution_indexes.add(idx)
            else:
                for idx, seed in zip(instagram_batch_indexes, instagram_seeds):
                    resolved[idx] = (raw_inputs[idx], seed)

        for idx, raw_input in enumerate(raw_inputs):
            if idx in instagram_batch_index_set:
                continue
            try:
                seed = await asyncio.to_thread(resolve_exact_seed, raw_input, context=context)
            except Exception as exc:
                errors.append(f"{raw_input}: не смог подтвердить профиль ({exc})")
                failed_resolution_indexes.add(idx)
                continue
            resolved[idx] = (raw_input, seed)

        for idx, (raw_input, seed) in enumerate(resolved):
            if seed is None:
                if idx not in failed_resolution_indexes:
                    errors.append(f"{raw_input}: не смог подтвердить профиль")
                continue
            try:
                if seed.platform == Platform.YOUTUBE:
                    passes_gate, recent_count = await asyncio.to_thread(
                        youtube_profile_recent_shorts_gate_status,
                        external_id=seed.external_id,
                        handle=seed.handle,
                        url=seed.url,
                        display_name=seed.title,
                        context=context,
                    )
                    if not passes_gate:
                        errors.append(
                            f"{seed.title or seed.external_id}: YouTube-канал не прошел фильтр активности "
                            f"(shorts за 60 дней: {recent_count}, нужно минимум 2)"
                        )
                        continue
                await db_call(
                    upsert_competitor,
                    user=user,
                    platform=seed.platform,
                    external_id=seed.external_id,
                    handle=seed.handle,
                    url=seed.url,
                    display_name=seed.title,
                    added_by=AddedBy.MANUAL,
                    meta=_seed_meta(seed),
                )
                counts[seed.platform] = counts.get(seed.platform, 0) + 1
                added += 1
            except Exception as exc:
                errors.append(f"{raw_input}: не смог добавить профиль ({exc})")

        skipped = max(0, len(raw_inputs) - added - len(errors))
        await message.answer(
            _build_add_remove_summary(
                action="Добавлено вручную",
                changed=added,
                skipped=skipped,
                errors=errors,
                counts=counts,
            )
        )
    except Exception:
        logger.exception("manual_competitor_add_failed user_id=%s", data.get("user_id"))
        await message.answer("Не удалось обработать список профилей. Попробуй еще раз позже.")
    finally:
        await state.clear()


def _build_add_remove_summary(
    *,
    action: str,
    changed: int,
    skipped: int,
    errors: list[str],
    counts: dict[str, int],
) -> str:
    lines = [
        f"{action}: {changed}",
        f"Пропущено: {skipped}",
        f"Ошибки: {len(errors)}",
        *_counts_lines(counts),
    ]
    if errors:
        lines.append("")
        lines.extend(errors[:5])
    return "\n".join(lines)
