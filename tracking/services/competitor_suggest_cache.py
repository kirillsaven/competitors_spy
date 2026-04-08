from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from time import perf_counter
from typing import Callable

from django.conf import settings

from tracking.adapters.base import SeedResolution
from tracking.models import AddedBy, SeedProfile, SeedStatus, TgUser, UserCompetitor, UserLinkedAccount
from tracking.services.competitor_service import list_active_user_competitor_links
from tracking.services.platform_onboarding import discover_competitors_for_onboarding
from tracking.services.setup_retry_cache import get_cached_retry_value, store_retry_value
from tracking.services.setup_runtime import SetupRunContext

_COMPETITOR_SUGGEST_CACHE_VERSION = "v1"
COMPETITOR_SUGGEST_CACHE_SOURCE_COLD_BUILD = "cold_build"
COMPETITOR_SUGGEST_CACHE_SOURCE_SETUP_WARM = "warmed_from_setup"
COMPETITOR_SUGGEST_CACHE_SOURCE_SNAPSHOT_HIT = "snapshot_hit"


@dataclass(frozen=True)
class CompetitorSuggestCacheLoadResult:
    candidates: list[dict]
    notes: list[str]
    cache_hit: bool
    cache_source: str
    discovery_build_ms: float


def _discovery_target_per_platform() -> int:
    return int(
        getattr(
            settings,
            "DISCOVERY_TARGET_COMPETITORS_PER_PLATFORM",
            getattr(settings, "MAX_COMPETITORS_PER_PLATFORM", 20),
        )
        or 20
    )


def _competitor_suggest_cache_ttl_seconds() -> int:
    return max(1, int(getattr(settings, "COMPETITOR_SUGGEST_CACHE_TTL_SECONDS", 900) or 900))


def _resolved_seed_profile_for_user(*, user: TgUser) -> SeedProfile:
    seed_profile = (
        SeedProfile.objects.filter(user=user, status=SeedStatus.RESOLVED)
        .order_by("-id")
        .first()
    )
    if seed_profile is None:
        raise RuntimeError("Не нашел сохраненный setup. Сначала заново заверши /setup.")
    return seed_profile


def _seed_input_and_keywords(*, seed_profile: SeedProfile) -> tuple[str, list[str]]:
    seed_input = str(seed_profile.canonical_url or seed_profile.raw_input or "").strip()
    if not seed_input:
        raise RuntimeError("В setup не сохранился исходный профиль. Запусти /setup заново.")
    keywords = [str(item).strip() for item in (seed_profile.niche_keywords or []) if str(item).strip()]
    if not keywords:
        raise RuntimeError("Не нашел ключевые слова ниши. Запусти /setup заново.")
    return seed_input, keywords


def _linked_account_cache_snapshot(account: UserLinkedAccount) -> dict:
    return {
        "platform": str(account.platform or ""),
        "external_id": str(account.external_id or "").strip(),
        "handle": str(account.handle or "").strip(),
        "url": str(account.url or "").strip(),
        "display_name": str(account.display_name or "").strip(),
        "source": str(account.source or ""),
        "is_seed": bool(account.is_seed),
        "meta": account.meta if isinstance(account.meta, dict) else {},
    }


def _active_competitor_cache_snapshot(link: UserCompetitor) -> dict:
    competitor = link.competitor
    return {
        "platform": str(competitor.platform or ""),
        "external_id": str(competitor.external_id or "").strip(),
        "handle": str(competitor.handle or "").strip(),
        "url": str(competitor.url or "").strip(),
        "display_name": str(competitor.display_name or "").strip(),
        "meta": competitor.meta if isinstance(competitor.meta, dict) else {},
    }


def build_competitor_suggest_cache_key(*, user: TgUser) -> str:
    seed_profile = _resolved_seed_profile_for_user(user=user)
    seed_input, keywords = _seed_input_and_keywords(seed_profile=seed_profile)
    linked_accounts = list(UserLinkedAccount.objects.filter(user=user).order_by("id"))
    active_links = list_active_user_competitor_links(user=user)
    payload = {
        "version": _COMPETITOR_SUGGEST_CACHE_VERSION,
        "user_id": int(user.id),
        "seed_profile": {
            "id": int(seed_profile.id),
            "raw_input": str(seed_profile.raw_input or "").strip(),
            "canonical_url": str(seed_profile.canonical_url or "").strip(),
            "detected_platform": str(seed_profile.detected_platform or ""),
            "niche_keywords": keywords,
            "niche_source": str(seed_profile.niche_source or ""),
            "seed_input": seed_input,
        },
        "linked_accounts": [_linked_account_cache_snapshot(account) for account in linked_accounts],
        "active_competitors": [_active_competitor_cache_snapshot(link) for link in active_links],
        "yt_max_search_calls": int(getattr(settings, "YT_MAX_SEARCH_CALLS_PER_SETUP", 5) or 5),
        "max_candidates_per_platform": _discovery_target_per_platform(),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return f"competitor-suggest::{digest}"


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


def build_competitor_suggest_candidates(*, user: TgUser) -> tuple[list[dict], list[str]]:
    seed_profile = _resolved_seed_profile_for_user(user=user)
    seed_input, keywords = _seed_input_and_keywords(seed_profile=seed_profile)

    from tracking.services.seed_resolver import resolve_exact_seed

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


def store_competitor_suggest_cache(
    *,
    user: TgUser,
    candidates: list[dict],
    notes: list[str],
    cache_source: str,
) -> None:
    store_retry_value(
        build_competitor_suggest_cache_key(user=user),
        {
            "candidates": deepcopy(candidates),
            "notes": list(notes),
            "cache_source": str(cache_source or COMPETITOR_SUGGEST_CACHE_SOURCE_COLD_BUILD),
        },
        ttl_seconds=_competitor_suggest_cache_ttl_seconds(),
    )


def load_competitor_suggest_cache(
    *,
    user: TgUser,
    builder: Callable[..., tuple[list[dict], list[str]]],
) -> CompetitorSuggestCacheLoadResult:
    cache_key = build_competitor_suggest_cache_key(user=user)
    cached, cache_hit = get_cached_retry_value(cache_key)
    if cache_hit and isinstance(cached, dict):
        candidates = deepcopy(list(cached.get("candidates") or []))
        notes = [str(item) for item in (cached.get("notes") or []) if str(item).strip()]
        stored_source = str(cached.get("cache_source") or "").strip()
        cache_source = (
            COMPETITOR_SUGGEST_CACHE_SOURCE_SETUP_WARM
            if stored_source == COMPETITOR_SUGGEST_CACHE_SOURCE_SETUP_WARM
            else COMPETITOR_SUGGEST_CACHE_SOURCE_SNAPSHOT_HIT
        )
        return CompetitorSuggestCacheLoadResult(
            candidates=candidates,
            notes=notes,
            cache_hit=True,
            cache_source=cache_source,
            discovery_build_ms=0.0,
        )

    discovery_started = perf_counter()
    candidates, notes = builder(user=user)
    discovery_build_ms = (perf_counter() - discovery_started) * 1000
    store_competitor_suggest_cache(
        user=user,
        candidates=candidates,
        notes=notes,
        cache_source=COMPETITOR_SUGGEST_CACHE_SOURCE_COLD_BUILD,
    )
    return CompetitorSuggestCacheLoadResult(
        candidates=deepcopy(candidates),
        notes=list(notes),
        cache_hit=False,
        cache_source=COMPETITOR_SUGGEST_CACHE_SOURCE_COLD_BUILD,
        discovery_build_ms=discovery_build_ms,
    )


def warm_competitor_suggest_cache(
    *,
    user: TgUser,
    builder: Callable[..., tuple[list[dict], list[str]]],
) -> CompetitorSuggestCacheLoadResult:
    cache_key = build_competitor_suggest_cache_key(user=user)
    cached, cache_hit = get_cached_retry_value(cache_key)
    if cache_hit and isinstance(cached, dict):
        candidates = deepcopy(list(cached.get("candidates") or []))
        notes = [str(item) for item in (cached.get("notes") or []) if str(item).strip()]
        return CompetitorSuggestCacheLoadResult(
            candidates=candidates,
            notes=notes,
            cache_hit=True,
            cache_source=COMPETITOR_SUGGEST_CACHE_SOURCE_SNAPSHOT_HIT,
            discovery_build_ms=0.0,
        )

    discovery_started = perf_counter()
    candidates, notes = builder(user=user)
    discovery_build_ms = (perf_counter() - discovery_started) * 1000
    store_competitor_suggest_cache(
        user=user,
        candidates=candidates,
        notes=notes,
        cache_source=COMPETITOR_SUGGEST_CACHE_SOURCE_SETUP_WARM,
    )
    return CompetitorSuggestCacheLoadResult(
        candidates=deepcopy(candidates),
        notes=list(notes),
        cache_hit=False,
        cache_source=COMPETITOR_SUGGEST_CACHE_SOURCE_SETUP_WARM,
        discovery_build_ms=discovery_build_ms,
    )
