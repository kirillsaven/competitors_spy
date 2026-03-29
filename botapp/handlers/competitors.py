from __future__ import annotations

import asyncio

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from django.conf import settings

from botapp.db import db_call, db_run
from botapp.state import CompetitorManagementStates
from botapp.user_sync import upsert_tg_user
from tracking.adapters.base import SeedResolution
from tracking.models import AddedBy, Schedule, TgUser, UserCompetitor
from tracking.services.competitor_service import (
    PLATFORM_LABELS,
    PLATFORM_ORDER,
    deactivate_user_competitors,
    find_active_competitor_link_match,
    get_active_user_competitor_counts,
    list_active_user_competitor_links,
    list_active_user_competitor_links_grouped,
    upsert_competitor,
)
from tracking.services.seed_resolver import SeedResolveError, resolve_exact_seed

router = Router()


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


def _active_link_by_key(*, active_links: list[UserCompetitor]) -> dict[tuple[str, str], UserCompetitor]:
    return {
        (str(link.competitor.platform or ""), str(link.competitor.external_id or "")): link
        for link in active_links
    }


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
    lines.append("/competitors_add - добавить вручную")
    lines.append("/competitors_remove - убрать из списка")
    await message.answer("\n".join(lines))


@router.message(Command("competitors_add"))
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

    await state.set_state(CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT)
    await message.answer(
        "Пришли ссылки, хендлы или никнеймы по одному в строке.\n"
        "Я добавлю найденные профили в активный список."
    )


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

    await state.set_state(CompetitorManagementStates.WAIT_COMPETITORS_REMOVE_INPUT)
    await message.answer(
        "Пришли ссылки, хендлы или никнеймы по одному в строке.\n"
        "Я выключу их только из твоего активного списка."
    )


@router.message(CompetitorManagementStates.WAIT_COMPETITORS_ADD_INPUT)
async def on_competitors_add_input(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw or not message.from_user:
        await message.answer("Пришли хотя бы одну ссылку или хендл, по одному в строке.")
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
    counts = await db_run(lambda: get_active_user_competitor_counts(user=user))
    active_keys = {
        (str(link.competitor.platform or ""), str(link.competitor.external_id or ""))
        for link in await db_run(lambda: list_active_user_competitor_links(user=user))
    }
    limit = int(getattr(settings, "MAX_COMPETITORS_PER_PLATFORM", 20))
    added = 0
    skipped = 0
    errors: list[str] = []

    for line in [item.strip() for item in raw.splitlines() if item.strip()]:
        try:
            seed = await asyncio.to_thread(resolve_exact_seed, line)
        except SeedResolveError as exc:
            errors.append(f"{line}: {exc}")
            continue
        except Exception as exc:
            errors.append(f"{line}: {exc}")
            continue
        if not seed:
            errors.append(f"{line}: профиль не подтвержден")
            continue

        key = (seed.platform, seed.external_id)
        if key in active_keys:
            skipped += 1
            continue
        if counts.get(seed.platform, 0) >= limit:
            errors.append(f"{line}: достигнут лимит для {PLATFORM_LABELS.get(seed.platform, seed.platform)} ({limit})")
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
        active_keys.add(key)
        counts[seed.platform] = counts.get(seed.platform, 0) + 1
        added += 1

    await state.set_state(None)
    await message.answer(_build_add_remove_summary(action="Добавлено", changed=added, skipped=skipped, errors=errors, counts=counts))


@router.message(CompetitorManagementStates.WAIT_COMPETITORS_REMOVE_INPUT)
async def on_competitors_remove_input(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw or not message.from_user:
        await message.answer("Пришли хотя бы одну ссылку или хендл, по одному в строке.")
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
    active_links = await db_run(lambda: list_active_user_competitor_links(user=user))
    active_by_key = _active_link_by_key(active_links=active_links)
    removed_ids: list[int] = []
    removed = 0
    skipped = 0
    errors: list[str] = []

    for line in [item.strip() for item in raw.splitlines() if item.strip()]:
        match = find_active_competitor_link_match(active_links=list(active_by_key.values()), raw_input=line)
        if match is None:
            try:
                seed = await asyncio.to_thread(resolve_exact_seed, line)
            except SeedResolveError as exc:
                errors.append(f"{line}: {exc}")
                continue
            except Exception as exc:
                errors.append(f"{line}: {exc}")
                continue
            if not seed:
                skipped += 1
                continue
            match = active_by_key.pop((seed.platform, seed.external_id), None)
            if match is None:
                skipped += 1
                continue
        else:
            active_by_key.pop((match.competitor.platform, match.competitor.external_id), None)

        removed_ids.append(match.competitor_id)
        removed += 1

    await db_run(lambda: deactivate_user_competitors(user=user, competitor_ids=removed_ids))
    counts = await db_run(lambda: get_active_user_competitor_counts(user=user))
    await state.set_state(None)
    await message.answer(_build_add_remove_summary(action="Удалено", changed=removed, skipped=skipped, errors=errors, counts=counts))


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
