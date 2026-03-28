from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
import re
from typing import Any

from django.conf import settings

from tracking.adapters.base import CompetitorCandidate, SeedResolution
from tracking.adapters.instagram import (
    ApifyInstagramClient,
    InstagramApiError,
    build_profile_url as build_instagram_profile_url,
    extract_handle,
    profile_to_video_details,
)
from tracking.adapters.tiktok import (
    ApifyTikTokClient,
    TikTokApiError,
    build_profile_url as build_tiktok_profile_url,
    item_to_video_details,
)
from tracking.adapters.youtube import YouTubeApiError, playlist_items_to_video_ids, video_items_to_details
from tracking.models import Platform
from tracking.services.provider_config import get_instagram_apify_config, get_tiktok_apify_config
from tracking.services.provider_runtime import log_provider_call
from tracking.services.setup_retry_cache import get_cached_retry_value, store_retry_value
from tracking.services.setup_runtime import (
    PLATFORM_STATE_ERROR,
    PLATFORM_STATE_SKIPPED,
    PLATFORM_STATE_UNAVAILABLE,
    SetupRunContext,
    get_platform_state,
    mark_platform_available,
    mark_platform_failure,
    mark_platform_skipped,
    platform_is_blocked,
)
from tracking.services.youtube_service import YouTubeNotConfigured, get_recent_video_titles, get_youtube_client


class PlatformOnboardingError(RuntimeError):
    pass


DISCOVERY_FOUND = "FOUND"
DISCOVERY_EMPTY = "EMPTY"
DISCOVERY_ERROR = "ERROR"
DISCOVERY_UNAVAILABLE = PLATFORM_STATE_UNAVAILABLE
DISCOVERY_SKIPPED = PLATFORM_STATE_SKIPPED

_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_GENERIC_QUERY_TOKENS = {
    "account",
    "accounts",
    "channel",
    "creator",
    "official",
    "profile",
    "video",
    "videos",
    "youtube",
    "instagram",
    "tiktok",
    "english",
    "канал",
    "официальный",
    "профиль",
}
_RU_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "ого",
    "его",
    "ему",
    "ому",
    "ыми",
    "ими",
    "иях",
    "ах",
    "ях",
    "ия",
    "ий",
    "ый",
    "ой",
    "ая",
    "ое",
    "ые",
    "ие",
    "ом",
    "ем",
    "ам",
    "ям",
    "ов",
    "ев",
    "ей",
    "ую",
    "юю",
    "ть",
    "ти",
    "ся",
    "сь",
    "а",
    "я",
    "ы",
    "и",
    "е",
    "о",
    "у",
)
_EN_SUFFIXES = (
    "ments",
    "ation",
    "ities",
    "ings",
    "ment",
    "ions",
    "tion",
    "ness",
    "able",
    "ible",
    "ers",
    "ing",
    "ies",
    "ied",
    "est",
    "er",
    "ed",
    "ly",
    "es",
    "s",
)

_DISCOVERY_QUERY_BUDGET = {
    Platform.YOUTUBE: 5,
    Platform.INSTAGRAM: 4,
    Platform.TIKTOK: 4,
}
_DISCOVERY_INITIAL_QUERY_BUDGET = {
    Platform.YOUTUBE: 1,
    Platform.INSTAGRAM: 1,
    Platform.TIKTOK: 1,
}
_DISCOVERY_RESULT_BUDGET = {
    Platform.YOUTUBE: 50,
    Platform.INSTAGRAM: 50,
    Platform.TIKTOK: 20,
}
_DISCOVERY_EARLY_STOP_CANDIDATES = {
    Platform.YOUTUBE: 20,
    Platform.INSTAGRAM: 20,
    Platform.TIKTOK: 20,
}
_DISCOVERY_VALIDATION_BUDGET = {
    Platform.YOUTUBE: 40,
    Platform.INSTAGRAM: 40,
    Platform.TIKTOK: 25,
}
_DISCOVERY_VALIDATION_MAX_SCAN = {
    Platform.YOUTUBE: 140,
    Platform.INSTAGRAM: 160,
    Platform.TIKTOK: 80,
}
_DISCOVERY_VALIDATION_ITEMS = 5
_DISCOVERY_MIN_RECENT_SHORTS = 2
_COLLECTIBLE_CACHE_VERSION = "uploads-v2"
_DISCOVERY_MIN_RECENT_ITEMS_BY_PLATFORM = {
    Platform.YOUTUBE: 2,
    Platform.INSTAGRAM: 1,
    Platform.TIKTOK: 2,
}
_DISCOVERY_RECENT_WINDOW_DAYS = 60
_DISCOVERY_YOUTUBE_UPLOAD_SCAN_LIMIT = 30
_YOUTUBE_LOW_RECALL_RESCUE_THRESHOLD = 12
_INSTAGRAM_RELATED_EXPANSION_PROFILES = 20
_INSTAGRAM_RELATED_PER_PROFILE = 12
_INSTAGRAM_GRAPH_EXPANSION_DEPTH = 2
_GENERIC_DISCOVERY_STEMS = {
    "coach",
    "course",
    "ege",
    "education",
    "educat",
    "group",
    "groups",
    "learn",
    "lesson",
    "mentor",
    "oge",
    "online",
    "prep",
    "school",
    "student",
    "study",
    "teach",
    "teacher",
    "tutor",
    "егэ",
    "групп",
    "занят",
    "обуч",
    "огэ",
    "онлайн",
    "преподав",
    "преподавател",
    "репетитор",
    "студент",
    "урок",
    "учеб",
    "ученик",
    "учител",
    "школ",
}
_TEACHER_STEMS = {"teacher", "teach", "tutor", "mentor", "репетитор", "преподав", "преподавател", "учител"}
_LESSON_STEMS = {"lesson", "lessons", "study", "course", "урок", "обуч", "курс"}
_GROUP_STEMS = {"group", "groups", "student", "students", "групп", "ученик", "студент"}
_SCHOOL_STEMS = {"school", "schools", "школ", "academy", "course", "courses"}
_SUBJECT_ALIAS_STEMS = {
    "английск": {"english"},
    "english": {"английск"},
    "дота": {"dota", "dota2", "дота2"},
    "дота2": {"дота", "dota", "dota2"},
    "dota": {"дота", "dota2", "дота2"},
    "dota2": {"dota", "дота", "дота2"},
}
_ENGLISH_SUBJECT_STEMS = {"английск", "english"}
_ENGLISH_AFFINITY_STEMS = _ENGLISH_SUBJECT_STEMS | {"ielts", "toefl", "celta", "delta", "cae", "cpe", "esl", "tefl"}
_ENGLISH_CONFLICT_STEMS = {
    "deutsch",
    "german",
    "spanish",
    "french",
    "немецк",
    "испан",
    "китайск",
    "корейск",
    "француз",
}
_INSTRUCTIONAL_CONTENT_STEMS = {
    "experi",
    "guide",
    "guid",
    "experiment",
    "experiments",
    "lesson",
    "vocab",
    "vocabulary",
    "phrase",
    "phrases",
    "grammar",
    "pronunciation",
    "tutorial",
    "tips",
    "гайд",
    "граммат",
    "лексик",
    "объясн",
    "предлог",
    "произнош",
    "разбор",
    "словар",
    "слово",
    "урок",
    "фраз",
    "выражен",
    "конструкц",
    "правил",
}
_SELF_REFERENTIAL_QUERY_STEMS = {
    "i",
    "me",
    "my",
    "our",
    "ours",
    "we",
    "мен",
    "мне",
    "мой",
    "мо",
    "мы",
    "наш",
    "открыва",
    "открываю",
    "помога",
    "помогаю",
    "сво",
    "свою",
}
_IDENTITY_FILTER_EXEMPT_STEMS = {
    "analysi",
    "business",
    "creator",
    "doctor",
    "dota",
    "entrepreneur",
    "evidence",
    "experiment",
    "finance",
    "invest",
    "lesson",
    "macro",
    "making",
    "market",
    "money",
    "product",
    "productiv",
    "psychology",
    "research",
    "science",
    "school",
    "strateg",
    "teacher",
    "tool",
}
_QUERY_COMPRESSION_STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "from",
    "how",
    "if",
    "in",
    "into",
    "of",
    "on",
    "our",
    "that",
    "the",
    "this",
    "to",
    "with",
    "you",
    "your",
    "как",
    "в",
    "для",
    "и",
    "на",
    "по",
    "с",
}
_QUERY_LEAD_NOISE_STEMS = {
    "comment",
    "consider",
    "discover",
    "explor",
    "follow",
    "join",
    "learn",
    "sound",
    "subscrib",
    "watch",
}


@dataclass(frozen=True)
class PlatformDiscoveryStatus:
    platform: str
    status: str
    candidate_count: int = 0
    reason: str = ""


@dataclass(frozen=True)
class DiscoveryOutcome:
    candidates: list[CompetitorCandidate]
    platform_statuses: list[PlatformDiscoveryStatus]
    notes: list[str]


@dataclass
class _DiscoveryCandidate:
    platform: str
    external_id: str
    handle: str | None
    url: str
    display_name: str | None
    description: str = ""
    query_hits: set[str] = field(default_factory=set)
    cross_platform_keys: set[str] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def sort_tiebreak(self) -> tuple[int, str, str]:
        return (
            int(self.metadata.get("rank_hint") or 0),
            str(self.display_name or "").lower(),
            self.external_id,
        )


def _merge_discovery_candidates(*candidate_sets: list[_DiscoveryCandidate]) -> list[_DiscoveryCandidate]:
    merged: dict[tuple[str, str], _DiscoveryCandidate] = {}
    for candidates in candidate_sets:
        for candidate in candidates:
            key = (candidate.platform, candidate.external_id)
            existing = merged.get(key)
            if existing is None:
                merged[key] = candidate
                continue
            existing_graph_hits = int(existing.metadata.get("graph_hits") or 0)
            candidate_graph_hits = int(candidate.metadata.get("graph_hits") or 0)
            existing_graph_depth = int(existing.metadata.get("graph_depth") or 0)
            candidate_graph_depth = int(candidate.metadata.get("graph_depth") or 0)
            existing.query_hits.update(candidate.query_hits)
            existing.cross_platform_keys.update(candidate.cross_platform_keys)
            existing.metadata.update(candidate.metadata)
            if existing_graph_hits or candidate_graph_hits:
                existing.metadata["graph_hits"] = existing_graph_hits + candidate_graph_hits
            if candidate_graph_depth:
                existing.metadata["graph_depth"] = (
                    min(existing_graph_depth, candidate_graph_depth)
                    if existing_graph_depth
                    else candidate_graph_depth
                )
            if not existing.display_name and candidate.display_name:
                existing.display_name = candidate.display_name
            if not existing.description and candidate.description:
                existing.description = candidate.description
            if not existing.handle and candidate.handle:
                existing.handle = candidate.handle
            if not existing.url and candidate.url:
                existing.url = candidate.url
    return list(merged.values())


def _platform_label(platform: str) -> str:
    return {
        Platform.YOUTUBE: "YouTube",
        Platform.TIKTOK: "TikTok",
        Platform.INSTAGRAM: "Instagram",
    }.get(str(platform or ""), str(platform or "Platform"))


def _render_platform_status(status: PlatformDiscoveryStatus) -> str:
    line = f"{_platform_label(status.platform)}: {status.status}"
    if status.status == DISCOVERY_FOUND:
        line += f" ({status.candidate_count})"
    if status.reason:
        line += f" — {status.reason}"
    return line


def _profile_cache_key(platform: str, lookup: str) -> str:
    return f"profile::{platform}::{str(lookup or '').strip().lower()}"


def _recent_cache_key(platform: str, external_id: str, handle: str | None, n: int) -> str:
    identity = str(handle or external_id or "").strip().lower()
    return f"recent::{_COLLECTIBLE_CACHE_VERSION}::{platform}::{identity}::{max(1, int(n))}"


def _search_cache_key(platform: str, query: str) -> str:
    return f"search::{platform}::{' '.join(str(query or '').strip().lower().split())}"


def _retry_cache_key(*, layer: str, key: str) -> str:
    return f"setup-retry::{str(layer or '').strip()}::{str(key or '').strip()}"


def _collectible_signals_payload(
    *,
    texts: list[str],
    views: list[int],
    recent_count: int,
) -> tuple[list[str], list[int], int]:
    return (
        [str(text) for text in texts if str(text or "").strip()],
        [max(0, int(view)) for view in views],
        max(0, int(recent_count)),
    )


def _get_cached_collectible_signals(
    *,
    platform: str,
    external_id: str,
    handle: str | None,
    n: int,
    context: SetupRunContext | None = None,
) -> tuple[list[str], list[int], int] | None:
    cache_key = _recent_cache_key(platform, external_id, handle, n)
    if context is not None and cache_key in context.collectible_signals_cache:
        cached = context.collectible_signals_cache[cache_key]
        return _collectible_signals_payload(texts=list(cached[0]), views=list(cached[1]), recent_count=int(cached[2]))
    retry_cached, retry_found = get_cached_retry_value(_retry_cache_key(layer="collectible-signals", key=cache_key))
    if retry_found and isinstance(retry_cached, tuple) and len(retry_cached) == 3:
        texts, views, recent_count = retry_cached
        payload = _collectible_signals_payload(
            texts=list(texts) if isinstance(texts, list) else [],
            views=list(views) if isinstance(views, list) else [],
            recent_count=int(recent_count or 0),
        )
        if context is not None:
            context.collectible_signals_cache[cache_key] = payload
        return payload
    return None


def _store_cached_collectible_signals(
    *,
    platform: str,
    external_id: str,
    handle: str | None,
    n: int,
    texts: list[str],
    views: list[int],
    recent_count: int,
    context: SetupRunContext | None = None,
) -> tuple[list[str], list[int], int]:
    cache_key = _recent_cache_key(platform, external_id, handle, n)
    payload = _collectible_signals_payload(texts=texts, views=views, recent_count=recent_count)
    if context is not None:
        context.collectible_signals_cache[cache_key] = payload
    store_retry_value(_retry_cache_key(layer="collectible-signals", key=cache_key), payload)
    return payload


def _provider_context_id(*, context: SetupRunContext | None = None, context_id: str | None = None) -> str:
    explicit = str(context_id or "").strip()
    if explicit:
        return explicit
    return str(getattr(context, "trace_id", "") or "").strip()


def _search_cache_payload(*, items: list[dict[str, Any]], requested_limit: int) -> dict[str, Any]:
    return {
        "items": [item for item in items if isinstance(item, dict)],
        "requested_limit": max(1, int(requested_limit)),
    }


def _read_search_cache_payload(value: object) -> tuple[list[dict[str, Any]], int | None]:
    if isinstance(value, dict):
        items = [item for item in (value.get("items") or []) if isinstance(item, dict)]
        requested_limit = value.get("requested_limit")
        try:
            requested_limit_int = max(1, int(requested_limit)) if requested_limit is not None else None
        except (TypeError, ValueError):
            requested_limit_int = None
        return items, requested_limit_int
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)], None
    return [], None


def _search_cache_satisfies_request(*, items: list[dict[str, Any]], cached_limit: int | None, requested_limit: int) -> bool:
    if not items:
        return True
    if cached_limit is None:
        return True
    if len(items) >= requested_limit:
        return True
    return cached_limit >= requested_limit


def _normalize_instagram_lookup(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        return "", ""
    if raw.isdigit():
        return "id", raw
    handle = extract_handle(raw)
    if handle:
        return "handle", handle.lower()
    return "url", raw.rstrip("/").lower()


def _find_instagram_profile_for_lookup(profiles: list[dict[str, Any]], lookup: str) -> dict[str, Any] | None:
    lookup_kind, lookup_value = _normalize_instagram_lookup(lookup)
    for profile in profiles:
        username = str(profile.get("username") or "").strip().lower()
        profile_id = str(profile.get("id") or "").strip()
        profile_url = str(profile.get("url") or "").strip().rstrip("/").lower()
        if lookup_kind == "id" and profile_id == lookup_value:
            return profile
        if lookup_kind == "handle" and username == lookup_value:
            return profile
        if lookup_kind == "url" and profile_url == lookup_value:
            return profile
        if lookup_kind == "url":
            resolved_handle = extract_handle(lookup)
            if resolved_handle and username == resolved_handle.lower():
                return profile
    return None


def fetch_instagram_profiles_cached(
    *,
    inputs: list[str],
    context: SetupRunContext | None = None,
    purpose: str = "profile_lookup",
    context_id: str | None = None,
) -> list[dict[str, Any]]:
    sanitized_inputs = [value.strip() for value in inputs if value and value.strip()]
    if not sanitized_inputs:
        return []
    if platform_is_blocked(context, Platform.INSTAGRAM):
        state = get_platform_state(context, Platform.INSTAGRAM)
        raise PlatformOnboardingError(state.reason or "Instagram is unavailable for this setup")

    cached_profiles: list[dict[str, Any]] = []
    missing_inputs: list[str] = []
    config = get_instagram_apify_config()
    actor = str(getattr(config, "profile_actor_id", getattr(config, "actor_id", "")) or "")
    trace_id = _provider_context_id(context=context, context_id=context_id)
    for lookup in sanitized_inputs:
        cache_key = _profile_cache_key(Platform.INSTAGRAM, lookup)
        if context is not None and cache_key in context.profile_cache:
            cached = context.profile_cache[cache_key]
            if isinstance(cached, dict):
                cached_profiles.append(cached)
                log_provider_call(
                    actor=actor,
                    platform=Platform.INSTAGRAM,
                    purpose=purpose,
                    cache="runtime_hit",
                    normalized_input=lookup.strip().lower(),
                    requested_limit=None,
                    returned_count=1,
                    context_id=trace_id,
                )
            continue
        retry_cached, retry_found = get_cached_retry_value(_retry_cache_key(layer="instagram-profile", key=cache_key))
        if retry_found:
            if context is not None:
                context.profile_cache[cache_key] = retry_cached
            if isinstance(retry_cached, dict) and retry_cached:
                cached_profiles.append(retry_cached)
                log_provider_call(
                    actor=actor,
                    platform=Platform.INSTAGRAM,
                    purpose=purpose,
                    cache="ttl_hit",
                    normalized_input=lookup.strip().lower(),
                    requested_limit=None,
                    returned_count=1,
                    context_id=trace_id,
                )
            continue
        missing_inputs.append(lookup)

    if missing_inputs:
        client = _get_instagram_client()
        try:
            fetched = client.fetch_profiles(inputs=missing_inputs)
            mark_platform_available(context, Platform.INSTAGRAM)
        except (PlatformOnboardingError, InstagramApiError, RuntimeError) as exc:
            mark_platform_failure(context, platform=Platform.INSTAGRAM, reason=str(exc))
            raise
        finally:
            client.close()
        log_provider_call(
            actor=actor,
            platform=Platform.INSTAGRAM,
            purpose=purpose,
            cache="miss",
            normalized_input="|".join(sorted(value.strip().lower() for value in missing_inputs)),
            requested_limit=len(missing_inputs),
            returned_count=len([item for item in fetched if isinstance(item, dict)]),
            context_id=trace_id,
        )
        for lookup in missing_inputs:
            matched = _find_instagram_profile_for_lookup(fetched, lookup)
            store_retry_value(_retry_cache_key(layer="instagram-profile", key=_profile_cache_key(Platform.INSTAGRAM, lookup)), matched or {})
            if context is not None:
                context.profile_cache[_profile_cache_key(Platform.INSTAGRAM, lookup)] = matched or {}
            if matched:
                cached_profiles.append(matched)

    ordered: list[dict[str, Any]] = []
    for lookup in sanitized_inputs:
        if context is None:
            matched = _find_instagram_profile_for_lookup(cached_profiles, lookup)
            if matched:
                ordered.append(matched)
            continue
        cache_key = _profile_cache_key(Platform.INSTAGRAM, lookup)
        cached = context.profile_cache.get(cache_key)
        if isinstance(cached, dict) and cached:
            ordered.append(cached)
    return ordered


def fetch_tiktok_profile_feeds_cached(
    *,
    handles: list[str],
    results_per_page: int,
    context: SetupRunContext | None = None,
    purpose: str = "profile_feed",
    context_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    normalized_handles: list[str] = []
    seen_handles: set[str] = set()
    for raw in handles:
        normalized = str(raw or "").strip()
        if not normalized or normalized in seen_handles:
            continue
        seen_handles.add(normalized)
        normalized_handles.append(normalized)
    if not normalized_handles:
        return {}
    if platform_is_blocked(context, Platform.TIKTOK):
        state = get_platform_state(context, Platform.TIKTOK)
        raise PlatformOnboardingError(state.reason or "TikTok is unavailable for this setup")

    requested = max(1, int(results_per_page))
    config = get_tiktok_apify_config()
    actor = str(getattr(config, "profile_actor_id", getattr(config, "actor_id", "")) or "")
    trace_id = _provider_context_id(context=context, context_id=context_id)
    grouped: dict[str, list[dict[str, Any]]] = {}
    missing_handles: list[str] = []

    for normalized_handle in normalized_handles:
        cache_key = _profile_cache_key(Platform.TIKTOK, normalized_handle)
        cached_items = context.profile_cache.get(cache_key) if context is not None else None
        if isinstance(cached_items, list) and len(cached_items) >= requested:
            grouped[normalized_handle] = [item for item in cached_items[:requested] if isinstance(item, dict)]
            log_provider_call(
                actor=actor,
                platform=Platform.TIKTOK,
                purpose=purpose,
                cache="runtime_hit",
                normalized_input=normalized_handle.lower(),
                requested_limit=requested,
                returned_count=len(grouped[normalized_handle]),
                context_id=trace_id,
            )
            continue
        retry_cached, retry_found = get_cached_retry_value(_retry_cache_key(layer="tiktok-feed", key=cache_key))
        if retry_found and isinstance(retry_cached, list) and len(retry_cached) >= requested:
            if context is not None:
                context.profile_cache[cache_key] = list(retry_cached)
            grouped[normalized_handle] = [item for item in retry_cached[:requested] if isinstance(item, dict)]
            log_provider_call(
                actor=actor,
                platform=Platform.TIKTOK,
                purpose=purpose,
                cache="ttl_hit",
                normalized_input=normalized_handle.lower(),
                requested_limit=requested,
                returned_count=len(grouped[normalized_handle]),
                context_id=trace_id,
            )
            continue
        missing_handles.append(normalized_handle)

    if missing_handles:
        client = _get_tiktok_client()
        try:
            if hasattr(client, "fetch_profile_feeds"):
                items = client.fetch_profile_feeds(handles=missing_handles, results_per_profile=requested)
            elif len(missing_handles) == 1:
                items = client.fetch_profile_feed(handle=missing_handles[0], results_per_page=requested)
            else:
                raise PlatformOnboardingError("TikTok client does not support batched profile feeds")
            mark_platform_available(context, Platform.TIKTOK)
        except (PlatformOnboardingError, TikTokApiError, RuntimeError) as exc:
            mark_platform_failure(context, platform=Platform.TIKTOK, reason=str(exc))
            raise
        finally:
            client.close()

        grouped_fetched: dict[str, list[dict[str, Any]]] = {handle: [] for handle in missing_handles}
        for item in items:
            if not isinstance(item, dict):
                continue
            author_meta = item.get("authorMeta") or {}
            item_handle = str(author_meta.get("name") or "").strip()
            normalized_item_handle = _normalize_handle(item_handle)
            matched_handle = next(
                (handle for handle in missing_handles if _normalize_handle(handle) == normalized_item_handle),
                None,
            )
            if matched_handle:
                grouped_fetched[matched_handle].append(item)

        log_provider_call(
            actor=actor,
            platform=Platform.TIKTOK,
            purpose=purpose,
            cache="miss",
            normalized_input="|".join(sorted(handle.lower() for handle in missing_handles)),
            requested_limit=requested,
            returned_count=len([item for item in items if isinstance(item, dict)]),
            context_id=trace_id,
        )

        for normalized_handle in missing_handles:
            found_items = [item for item in grouped_fetched.get(normalized_handle, [])[:requested] if isinstance(item, dict)]
            store_retry_value(_retry_cache_key(layer="tiktok-feed", key=_profile_cache_key(Platform.TIKTOK, normalized_handle)), list(found_items))
            if context is not None:
                context.profile_cache[_profile_cache_key(Platform.TIKTOK, normalized_handle)] = list(found_items)
            grouped[normalized_handle] = found_items

    return {handle: list(grouped.get(handle, [])) for handle in normalized_handles}


def fetch_tiktok_profile_feed_cached(
    *,
    handle: str,
    results_per_page: int,
    context: SetupRunContext | None = None,
    purpose: str = "profile_feed",
    context_id: str | None = None,
) -> list[dict[str, Any]]:
    normalized_handle = str(handle or "").strip()
    if not normalized_handle:
        return []
    return fetch_tiktok_profile_feeds_cached(
        handles=[normalized_handle],
        results_per_page=results_per_page,
        context=context,
        purpose=purpose,
        context_id=context_id,
    ).get(normalized_handle, [])


def _cached_youtube_search_channel_ids(
    *,
    query: str,
    max_results: int,
    context: SetupRunContext | None = None,
) -> list[str]:
    cache_key = _search_cache_key(Platform.YOUTUBE, query)
    if context is not None and cache_key in context.search_cache:
        cached = context.search_cache[cache_key]
        return [str(item) for item in cached[:max_results]]
    retry_cached, retry_found = get_cached_retry_value(_retry_cache_key(layer="youtube-search", key=cache_key))
    if retry_found and isinstance(retry_cached, list):
        if context is not None:
            context.search_cache[cache_key] = list(retry_cached)
        return [str(item) for item in retry_cached[:max_results]]
    client = get_youtube_client()
    try:
        channel_ids = list(client.search_channels(q=query, max_results=max_results))
        mark_platform_available(context, Platform.YOUTUBE)
    finally:
        client.close()
    if context is not None:
        context.search_cache[cache_key] = list(channel_ids)
    store_retry_value(
        _retry_cache_key(layer="youtube-search", key=cache_key),
        list(channel_ids),
        ttl_seconds=int(getattr(settings, "YOUTUBE_SEARCH_CACHE_TTL_SECONDS", 21600) or 21600),
    )
    return channel_ids


def _cached_instagram_search_results(
    *,
    query: str,
    limit: int,
    context: SetupRunContext | None = None,
    purpose: str = "discovery_search",
    context_id: str | None = None,
) -> list[dict[str, Any]]:
    cache_key = _search_cache_key(Platform.INSTAGRAM, query)
    config = get_instagram_apify_config()
    actor = str(getattr(config, "search_actor_id", "") or "")
    trace_id = _provider_context_id(context=context, context_id=context_id)
    if context is not None and cache_key in context.search_cache:
        cached_items, cached_limit = _read_search_cache_payload(context.search_cache[cache_key])
        if _search_cache_satisfies_request(items=cached_items, cached_limit=cached_limit, requested_limit=limit):
            log_provider_call(
                actor=actor,
                platform=Platform.INSTAGRAM,
                purpose=purpose,
                cache="runtime_hit",
                normalized_input=" ".join(query.strip().lower().split()),
                requested_limit=limit,
                returned_count=len(cached_items),
                context_id=trace_id,
            )
            return list(cached_items)
    retry_cached, retry_found = get_cached_retry_value(_retry_cache_key(layer="instagram-search", key=cache_key))
    if retry_found:
        retry_items, retry_limit = _read_search_cache_payload(retry_cached)
        if _search_cache_satisfies_request(items=retry_items, cached_limit=retry_limit, requested_limit=limit):
            if context is not None:
                context.search_cache[cache_key] = _search_cache_payload(
                    items=retry_items,
                    requested_limit=retry_limit or limit,
                )
            log_provider_call(
                actor=actor,
                platform=Platform.INSTAGRAM,
                purpose=purpose,
                cache="ttl_hit",
                normalized_input=" ".join(query.strip().lower().split()),
                requested_limit=limit,
                returned_count=len(retry_items),
                context_id=trace_id,
            )
            return list(retry_items)
    if platform_is_blocked(context, Platform.INSTAGRAM):
        state = get_platform_state(context, Platform.INSTAGRAM)
        raise PlatformOnboardingError(state.reason or "Instagram is unavailable for this setup")
    client = _get_instagram_client()
    try:
        try:
            results = client.search_profiles(query=query, limit=limit)
        except TypeError:
            results = client.search_profiles(query=query)
        mark_platform_available(context, Platform.INSTAGRAM)
    except (PlatformOnboardingError, InstagramApiError, RuntimeError) as exc:
        mark_platform_failure(context, platform=Platform.INSTAGRAM, reason=str(exc))
        raise
    finally:
        client.close()
    filtered = [item for item in results if isinstance(item, dict)]
    log_provider_call(
        actor=actor,
        platform=Platform.INSTAGRAM,
        purpose=purpose,
        cache="miss",
        normalized_input=" ".join(query.strip().lower().split()),
        requested_limit=limit,
        returned_count=len(filtered),
        context_id=trace_id,
    )
    if context is not None:
        context.search_cache[cache_key] = _search_cache_payload(items=filtered, requested_limit=limit)
    store_retry_value(
        _retry_cache_key(layer="instagram-search", key=cache_key),
        _search_cache_payload(items=filtered, requested_limit=limit),
    )
    return filtered


def _cached_tiktok_search_results(
    *,
    query: str,
    limit: int,
    context: SetupRunContext | None = None,
    purpose: str = "discovery_search",
    context_id: str | None = None,
) -> list[dict[str, Any]]:
    cache_key = _search_cache_key(Platform.TIKTOK, query)
    config = get_tiktok_apify_config()
    actor = str(getattr(config, "search_actor_id", "") or "")
    trace_id = _provider_context_id(context=context, context_id=context_id)
    if context is not None and cache_key in context.search_cache:
        cached_items, cached_limit = _read_search_cache_payload(context.search_cache[cache_key])
        if _search_cache_satisfies_request(items=cached_items, cached_limit=cached_limit, requested_limit=limit):
            log_provider_call(
                actor=actor,
                platform=Platform.TIKTOK,
                purpose=purpose,
                cache="runtime_hit",
                normalized_input=" ".join(query.strip().lower().split()),
                requested_limit=limit,
                returned_count=len(cached_items),
                context_id=trace_id,
            )
            return list(cached_items)
    retry_cached, retry_found = get_cached_retry_value(_retry_cache_key(layer="tiktok-search", key=cache_key))
    if retry_found:
        retry_items, retry_limit = _read_search_cache_payload(retry_cached)
        if _search_cache_satisfies_request(items=retry_items, cached_limit=retry_limit, requested_limit=limit):
            if context is not None:
                context.search_cache[cache_key] = _search_cache_payload(
                    items=retry_items,
                    requested_limit=retry_limit or limit,
                )
            log_provider_call(
                actor=actor,
                platform=Platform.TIKTOK,
                purpose=purpose,
                cache="ttl_hit",
                normalized_input=" ".join(query.strip().lower().split()),
                requested_limit=limit,
                returned_count=len(retry_items),
                context_id=trace_id,
            )
            return list(retry_items)
    if platform_is_blocked(context, Platform.TIKTOK):
        state = get_platform_state(context, Platform.TIKTOK)
        raise PlatformOnboardingError(state.reason or "TikTok is unavailable for this setup")
    client = _get_tiktok_client()
    try:
        try:
            results = client.search_profiles(query=query, limit=limit)
        except TypeError:
            results = client.search_profiles(query=query)
        except TikTokApiError as exc:
            if limit > 3 and "timed out" in str(exc).lower():
                retry_limit = max(3, limit // 2)
                results = client.search_profiles(query=query, limit=retry_limit)
            else:
                raise
        mark_platform_available(context, Platform.TIKTOK)
    except (PlatformOnboardingError, TikTokApiError, RuntimeError) as exc:
        mark_platform_failure(context, platform=Platform.TIKTOK, reason=str(exc))
        raise
    finally:
        client.close()
    filtered = [item for item in results if isinstance(item, dict)]
    log_provider_call(
        actor=actor,
        platform=Platform.TIKTOK,
        purpose=purpose,
        cache="miss",
        normalized_input=" ".join(query.strip().lower().split()),
        requested_limit=limit,
        returned_count=len(filtered),
        context_id=trace_id,
    )
    if context is not None:
        context.search_cache[cache_key] = _search_cache_payload(items=filtered, requested_limit=limit)
    store_retry_value(
        _retry_cache_key(layer="tiktok-search", key=cache_key),
        _search_cache_payload(items=filtered, requested_limit=limit),
    )
    return filtered


def _normalize_token(token: str) -> str:
    return str(token or "").strip().lower().replace("ё", "е")


def _stem_token(token: str) -> str:
    norm = _normalize_token(token)
    if len(norm) <= 4:
        return norm
    if re.search(r"[а-я]", norm):
        for suffix in _RU_SUFFIXES:
            if len(norm) - len(suffix) >= 4 and norm.endswith(suffix):
                return norm[: -len(suffix)]
        return norm
    for suffix in _EN_SUFFIXES:
        if len(norm) - len(suffix) >= 4 and norm.endswith(suffix):
            return norm[: -len(suffix)]
    return norm


def _token_stems(*values: str | None) -> set[str]:
    stems: set[str] = set()
    for value in values:
        for token in _TOKEN_RE.findall(str(value or "")):
            norm = _normalize_token(token)
            if len(norm) < 3 or norm in _GENERIC_QUERY_TOKENS:
                continue
            stems.add(_stem_token(norm))
    return stems


def _normalize_handle(value: str | None) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", _normalize_token(value or ""))


def _identity_keys(seed: SeedResolution | None, linked_accounts: list[SeedResolution] | None) -> tuple[set[tuple[str, str]], dict[str, set[str]], list[tuple[str, str]]]:
    excluded_ids: set[tuple[str, str]] = set()
    handles_by_platform: dict[str, set[str]] = {
        Platform.YOUTUBE: set(),
        Platform.TIKTOK: set(),
        Platform.INSTAGRAM: set(),
    }
    titles: list[tuple[str, str]] = []
    for account in [seed] + list(linked_accounts or []):
        if not account:
            continue
        if account.external_id:
            excluded_ids.add((account.platform, account.external_id))
        normalized_handle = _normalize_handle(account.handle)
        if normalized_handle:
            handles_by_platform.setdefault(account.platform, set()).add(normalized_handle)
        title = str(account.title or "").strip().lower()
        if title:
            titles.append((account.platform, title))
    return excluded_ids, handles_by_platform, titles


def _candidate_identity_key(candidate: _DiscoveryCandidate) -> str:
    handle = _normalize_handle(candidate.handle)
    if handle:
        return handle
    title_stems = sorted(_token_stems(candidate.display_name))
    return "|".join(title_stems[:3])


def _is_seed_like_candidate(
    candidate: _DiscoveryCandidate,
    *,
    excluded_ids: set[tuple[str, str]],
    handles_by_platform: dict[str, set[str]],
    seed_titles: list[tuple[str, str]],
) -> bool:
    if (candidate.platform, candidate.external_id) in excluded_ids:
        return True

    normalized_handle = _normalize_handle(candidate.handle)
    if normalized_handle and normalized_handle in handles_by_platform.get(candidate.platform, set()):
        return True

    candidate_title = str(candidate.display_name or "").strip().lower()
    if candidate_title:
        for platform, title in seed_titles:
            if platform != candidate.platform:
                continue
            if SequenceMatcher(None, candidate_title, title).ratio() >= 0.95:
                return True
    return False


def _query_stems(query: str) -> set[str]:
    return _token_stems(query)


def _theme_token_stems(*values: str | None) -> set[str]:
    excluded = set(_GENERIC_QUERY_TOKENS) - {"english"}
    stems: set[str] = set()
    for value in values:
        for token in _TOKEN_RE.findall(str(value or "")):
            norm = _normalize_token(token)
            if len(norm) < 3 or norm in excluded:
                continue
            stems.add(_stem_token(norm))
    return stems


def _expand_alias_stems(stems: set[str]) -> set[str]:
    expanded = set(stems)
    for stem in list(stems):
        expanded |= set(_SUBJECT_ALIAS_STEMS.get(stem, set()))
    return expanded


def _dedupe_keyword_queries(keywords: list[str], *, max_queries: int | None = None) -> list[str]:
    seen: set[str] = set()
    queries: list[str] = []
    for raw in keywords or []:
        query = " ".join(str(raw or "").split()).strip()
        if len(query) < 3:
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        queries.append(query)
        if max_queries is not None and len(queries) >= max_queries:
            break
    return queries


def _theme_phrase_stems(keywords: list[str]) -> list[tuple[set[str], set[str]]]:
    phrases: list[tuple[set[str], set[str]]] = []
    seen: set[tuple[str, ...]] = set()
    for query in _dedupe_keyword_queries(keywords, max_queries=8):
        full_stems = _theme_token_stems(query)
        if not full_stems:
            continue
        specific_stems = {stem for stem in full_stems if stem not in _GENERIC_DISCOVERY_STEMS}
        normalized = tuple(sorted(specific_stems or full_stems))
        if normalized in seen:
            continue
        seen.add(normalized)
        phrases.append((full_stems, specific_stems or full_stems))
    return phrases


def _keyword_substring_stems(*, text: str, keywords: list[str]) -> set[str]:
    normalized_text = re.sub(r"[^0-9a-zа-яё]+", "", _normalize_token(text))
    if len(normalized_text) < 6:
        return set()
    stems: set[str] = set()
    for query in _dedupe_keyword_queries(keywords, max_queries=8):
        for token in _TOKEN_RE.findall(str(query or "")):
            norm = _normalize_token(token)
            if len(norm) < 3 or norm in _GENERIC_QUERY_TOKENS:
                continue
            collapsed = re.sub(r"[^0-9a-zа-яё]+", "", norm)
            if len(collapsed) < 3:
                continue
            if collapsed in normalized_text:
                stems.add(_stem_token(norm))
    return stems


def _is_identity_like_query(
    query: str,
    *,
    full_stems: set[str],
    specific_stems: set[str],
    anchor_stems: set[str],
) -> bool:
    words = [part for part in str(query or "").split() if part]
    if len(words) < 2 or len(specific_stems) < 2:
        return False
    if specific_stems & _IDENTITY_FILTER_EXEMPT_STEMS:
        return False
    if not anchor_stems:
        return False
    if len(full_stems) != len(specific_stems):
        return False
    return len(specific_stems & anchor_stems) <= 0


def _theme_anchor_stems(keywords: list[str]) -> set[str]:
    counts: dict[str, int] = {}
    ordered_stems: list[str] = []
    for query in _dedupe_keyword_queries(keywords, max_queries=8):
        specific_stems = {stem for stem in _theme_token_stems(query) if stem not in _GENERIC_DISCOVERY_STEMS}
        if not specific_stems:
            continue
        for stem in specific_stems:
            counts[stem] = counts.get(stem, 0) + (2 if len(str(query or "").split()) == 1 else 1)
        for token in _TOKEN_RE.findall(str(query or "")):
            stem = _stem_token(token)
            if not stem or stem in _GENERIC_DISCOVERY_STEMS or stem in ordered_stems:
                continue
            ordered_stems.append(stem)
    anchors = {stem for stem, count in counts.items() if count >= 2}
    if anchors:
        return anchors
    if ordered_stems:
        return set(ordered_stems[:3])
    strongest = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {stem for stem, _count in strongest[:3]}


def _strong_phrase_overlap(stems: set[str], phrase_stems: set[str]) -> int:
    if not stems or not phrase_stems:
        return 0
    overlap = len(stems & phrase_stems)
    if overlap <= 0:
        return 0
    if len(phrase_stems) <= 1:
        return 1 if overlap >= 1 else 0
    required = min(2, len(phrase_stems))
    if overlap >= required or (overlap / float(len(phrase_stems))) >= 0.75:
        return overlap
    return 0


def _theme_specific_stems(keywords: list[str]) -> set[str]:
    out: set[str] = set()
    for _full, specific in _theme_phrase_stems(keywords):
        out |= specific
    return out


def _theme_agreement_metrics(*, texts: list[str], keywords: list[str]) -> dict[str, int]:
    phrase_defs = _theme_phrase_stems(keywords)
    anchor_stems = _theme_anchor_stems(keywords)
    if not phrase_defs:
        return {
            "anchor_overlap": 0,
            "full_overlap": 0,
            "specific_overlap": 0,
            "matched_phrases": 0,
            "strong_text_matches": 0,
        }

    text_stem_sets = []
    for text in texts:
        value = str(text or "").strip()
        if not value:
            continue
        stems = _theme_token_stems(value) | _keyword_substring_stems(text=value, keywords=keywords)
        text_stem_sets.append(_expand_alias_stems(stems))
    union_stems: set[str] = set()
    for stems in text_stem_sets:
        union_stems |= stems
    anchor_stems = _expand_alias_stems(anchor_stems)

    matched_phrases = 0
    anchor_overlap = len(union_stems & anchor_stems)
    specific_overlap = 0
    full_overlap = 0
    for full_stems, specific_stems in phrase_defs:
        full_stems = _expand_alias_stems(full_stems)
        specific_stems = _expand_alias_stems(specific_stems)
        if _strong_phrase_overlap(union_stems, specific_stems):
            matched_phrases += 1
        specific_overlap += len(union_stems & specific_stems)
        full_overlap += len(union_stems & full_stems)

    strong_text_matches = 0
    strong_anchor_matches = 0
    for stems in text_stem_sets:
        if any(_strong_phrase_overlap(stems, specific_stems) for _, specific_stems in phrase_defs):
            strong_text_matches += 1
        if anchor_stems and (stems & anchor_stems):
            strong_anchor_matches += 1

    return {
        "anchor_overlap": anchor_overlap,
        "full_overlap": full_overlap,
        "specific_overlap": specific_overlap,
        "matched_phrases": matched_phrases,
        "strong_text_matches": strong_text_matches,
        "strong_anchor_matches": strong_anchor_matches,
    }


def _is_english_teaching_keywords(keywords: list[str]) -> bool:
    stems = _expand_alias_stems(_theme_specific_stems(keywords))
    return bool(stems & _ENGLISH_SUBJECT_STEMS)


def _has_subject_conflict(*, texts: list[str], keywords: list[str]) -> bool:
    if not _is_english_teaching_keywords(keywords):
        return False
    stems = _expand_alias_stems(_theme_token_stems(" ".join(texts)))
    return bool(stems & _ENGLISH_CONFLICT_STEMS) and not bool(stems & _ENGLISH_SUBJECT_STEMS)


def _has_english_affinity_signal(*, texts: list[str]) -> bool:
    stems = _expand_alias_stems(_theme_token_stems(" ".join(texts)))
    return bool(stems & _ENGLISH_AFFINITY_STEMS)


def _theme_profile_passes(*, candidate: _DiscoveryCandidate, keywords: list[str]) -> bool:
    texts = [
        str(candidate.display_name or "").strip(),
        str(candidate.description or "").strip(),
        str(candidate.handle or "").strip(),
    ]
    if _has_subject_conflict(texts=texts, keywords=keywords):
        return False
    if _is_english_teaching_keywords(keywords) and not _has_english_affinity_signal(texts=texts):
        return False
    metrics = _theme_agreement_metrics(texts=texts, keywords=keywords)
    candidate.metadata["profile_theme_score"] = (
        metrics["matched_phrases"] * 3
        + metrics["specific_overlap"]
        + metrics["strong_text_matches"]
        + metrics["anchor_overlap"] * 2
    )
    candidate.metadata["profile_theme_matches"] = metrics["matched_phrases"]
    candidate.metadata["profile_theme_anchor_overlap"] = metrics["anchor_overlap"]
    candidate.metadata["profile_theme_specific_overlap"] = metrics["specific_overlap"]
    if metrics["anchor_overlap"] > 0 and (metrics["matched_phrases"] >= 1 or metrics["specific_overlap"] >= 2):
        return True
    graph_score = _instagram_graph_score(candidate)
    if graph_score >= 6:
        return (
            metrics["anchor_overlap"] >= 1
            or float(candidate.metadata.get("profile_theme_score") or 0) >= 4
            or (bool(candidate.metadata.get("verified")) and len(str(candidate.description or "").strip()) >= 12)
        )
    return False


def _theme_content_passes(
    *,
    platform: str,
    candidate: _DiscoveryCandidate,
    texts: list[str],
    keywords: list[str],
) -> bool:
    metrics = _theme_agreement_metrics(texts=texts, keywords=keywords)
    graph_score = _instagram_graph_score(candidate) if platform == Platform.INSTAGRAM else 0
    format_hits = len(_expand_alias_stems(_theme_token_stems(" ".join(texts))) & _INSTRUCTIONAL_CONTENT_STEMS)
    candidate.metadata["content_theme_score"] = (
        metrics["matched_phrases"] * 4
        + metrics["specific_overlap"]
        + metrics["strong_text_matches"] * 2
        + metrics["anchor_overlap"] * 2
        + metrics["strong_anchor_matches"] * 2
        + format_hits
    )
    candidate.metadata["content_theme_matches"] = metrics["matched_phrases"]
    candidate.metadata["content_theme_anchor_overlap"] = metrics["anchor_overlap"]
    candidate.metadata["content_theme_specific_overlap"] = metrics["specific_overlap"]
    candidate.metadata["content_theme_strong_text_matches"] = metrics["strong_text_matches"]
    candidate.metadata["content_theme_strong_anchor_matches"] = metrics["strong_anchor_matches"]
    candidate.metadata["content_format_hits"] = format_hits
    if _has_subject_conflict(texts=texts, keywords=keywords):
        return False
    if metrics["anchor_overlap"] <= 0 or metrics["strong_anchor_matches"] <= 0:
        if platform == Platform.INSTAGRAM and graph_score >= 9 and int(candidate.metadata.get("recent_collectible_count") or 0) >= 3:
            return True
        if platform == Platform.YOUTUBE:
            return (
                int(candidate.metadata.get("recent_collectible_count") or 0) >= _DISCOVERY_MIN_RECENT_ITEMS_BY_PLATFORM.get(
                    Platform.YOUTUBE,
                    _DISCOVERY_MIN_RECENT_SHORTS,
                )
                and (
                    (
                        float(candidate.metadata.get("profile_theme_score") or 0) >= 6
                        and (
                            metrics["specific_overlap"] >= 2
                            or metrics["matched_phrases"] >= 1
                        )
                    )
                    or (
                        _is_english_teaching_keywords(keywords)
                        and float(candidate.metadata.get("profile_theme_score") or 0) >= 18
                        and format_hits >= 2
                    )
                )
            )
        return (
            float(candidate.metadata.get("profile_theme_score") or 0) >= 10
            and int(candidate.metadata.get("recent_collectible_count") or 0) >= _DISCOVERY_MIN_RECENT_SHORTS
            and format_hits >= 2
        )
    if platform == Platform.INSTAGRAM:
        if (
            int(candidate.metadata.get("recent_collectible_count") or 0) == 1
            and metrics["matched_phrases"] >= 1
            and metrics["strong_anchor_matches"] >= 1
            and (
                metrics["specific_overlap"] >= 2
                or float(candidate.metadata.get("profile_theme_score") or 0) >= 8
            )
        ):
            return True
        if metrics["strong_text_matches"] >= 2:
            return True
        if (
            bool(candidate.metadata.get("verified"))
            and metrics["matched_phrases"] >= 2
            and metrics["specific_overlap"] >= 4
            and metrics["strong_anchor_matches"] >= 1
            and float(candidate.metadata.get("profile_theme_score") or 0) >= 8
        ):
            return True
        if _is_english_teaching_keywords(keywords):
            return format_hits >= 1 and metrics["strong_anchor_matches"] >= 2
        if graph_score >= 9 and int(candidate.metadata.get("recent_collectible_count") or 0) >= 3:
            return True
        if graph_score >= 7:
            return (
                int(candidate.metadata.get("recent_collectible_count") or 0) >= 1
                and (
                    metrics["strong_anchor_matches"] >= 1
                    or metrics["matched_phrases"] >= 1
                    or metrics["specific_overlap"] >= 2
                    or float(candidate.metadata.get("profile_theme_score") or 0) >= 6
                    or (
                        bool(candidate.metadata.get("verified"))
                        and int(candidate.metadata.get("recent_collectible_count") or 0) >= 2
                    )
                )
            )
        if graph_score >= 5:
            return (
                int(candidate.metadata.get("recent_collectible_count") or 0) >= 2
                and (
                    metrics["anchor_overlap"] >= 1
                    or float(candidate.metadata.get("profile_theme_score") or 0) >= 4
                    or bool(candidate.metadata.get("verified"))
                )
            )
        return (
            (metrics["matched_phrases"] >= 2 or metrics["specific_overlap"] >= 4)
            and metrics["strong_anchor_matches"] >= 2
            and (
                format_hits >= 1
                or float(candidate.metadata.get("profile_theme_score") or 0) >= 18
            )
        )
    return (
        (metrics["matched_phrases"] >= 1 and metrics["strong_text_matches"] >= 1)
        or metrics["specific_overlap"] >= 3
        or metrics["strong_text_matches"] >= 2
        or (
            float(candidate.metadata.get("profile_theme_score") or 0) >= 6
            and (
                metrics["specific_overlap"] >= 2
                or metrics["matched_phrases"] >= 1
            )
        )
        or (
            metrics["anchor_overlap"] >= 1
            and metrics["strong_anchor_matches"] >= 1
            and (
                metrics["specific_overlap"] >= 2
                or float(candidate.metadata.get("profile_theme_score") or 0) >= 6
                or format_hits >= 1
            )
        )
    )


def _candidate_search_overlap(candidate: _DiscoveryCandidate) -> int:
    candidate_stems = _token_stems(candidate.handle, candidate.display_name, candidate.description)
    overlap = 0
    for query in candidate.query_hits:
        overlap += len(candidate_stems & _query_stems(query))
    return overlap


def _instagram_graph_score(candidate: _DiscoveryCandidate) -> int:
    if candidate.platform != Platform.INSTAGRAM:
        return 0
    source = str(candidate.metadata.get("source") or "").strip().lower()
    depth = max(0, int(candidate.metadata.get("graph_depth") or 0))
    hits = max(0, int(candidate.metadata.get("graph_hits") or 0))
    score = min(hits, 3)
    if "seed_related" in candidate.query_hits:
        score += 4
    if source == "related_profile":
        score += 2
    if depth > 0:
        score += max(0, 3 - min(depth, 3))
    if bool(candidate.metadata.get("verified")):
        score += 1
    return score


def _instagram_keyword_stuffing_penalty(candidate: _DiscoveryCandidate) -> float:
    if candidate.platform != Platform.INSTAGRAM or _instagram_graph_score(candidate) > 0:
        return 0.0
    if bool(candidate.metadata.get("verified")) or len(str(candidate.description or "").strip()) >= 20:
        return 0.0
    handle_stems = _theme_token_stems(candidate.handle)
    display_stems = _theme_token_stems(candidate.display_name)
    query_stems: set[str] = set()
    for query in candidate.query_hits:
        query_stems |= _theme_token_stems(query)
    overlap = len((handle_stems | display_stems) & query_stems)
    noisy_handle = "_" in str(candidate.handle or "") or any(ch.isdigit() for ch in str(candidate.handle or ""))
    if overlap >= 3 and noisy_handle:
        return 18.0
    if overlap >= 4:
        return 14.0
    return 0.0


def _score_candidate(candidate: _DiscoveryCandidate) -> float:
    overlap = _candidate_search_overlap(candidate)
    query_repeat_bonus = len(candidate.query_hits) * 8.0
    overlap_bonus = overlap * 2.4
    description_bonus = 2.4 if len(str(candidate.description or "").strip()) >= 24 else 0.0
    cross_platform_bonus = len(candidate.cross_platform_keys) * 4.5
    verified_bonus = 5.0 if bool(candidate.metadata.get("verified")) else 0.0
    popularity_bonus = min(int(candidate.metadata.get("rank_hint") or 0), 5_000_000) / 100_000
    competitor_overlap_bonus = float(candidate.metadata.get("competitor_overlap") or 0) * 1.8
    collectible_count_bonus = min(int(candidate.metadata.get("collectible_count") or 0), 3) * 3.0
    recent_collectible_bonus = min(int(candidate.metadata.get("recent_collectible_count") or 0), 5) * 2.2
    collectible_views_bonus = min(int(candidate.metadata.get("collectible_views") or 0), 2_000_000) / 50_000
    profile_theme_bonus = float(candidate.metadata.get("profile_theme_score") or 0) * 2.0
    content_theme_bonus = float(candidate.metadata.get("content_theme_score") or 0) * 2.4
    graph_bonus = _instagram_graph_score(candidate) * 4.5
    stuffing_penalty = _instagram_keyword_stuffing_penalty(candidate)
    return (
        query_repeat_bonus
        + overlap_bonus
        + description_bonus
        + cross_platform_bonus
        + verified_bonus
        + popularity_bonus
        + competitor_overlap_bonus
        + collectible_count_bonus
        + recent_collectible_bonus
        + collectible_views_bonus
        + profile_theme_bonus
        + content_theme_bonus
        + graph_bonus
        - stuffing_penalty
    )


def _query_utility_score(query: str, *, anchor_stems: set[str]) -> int:
    stems = _theme_token_stems(query)
    if not stems:
        return 0
    specific_stems = {stem for stem in stems if stem not in _GENERIC_DISCOVERY_STEMS}
    generic_stems = stems - specific_stems
    words = [part for part in str(query or "").split() if part]
    word_count = len(words)
    anchor_overlap = len(specific_stems & anchor_stems)
    teacher_hits = len(stems & _TEACHER_STEMS)
    lesson_hits = len(stems & _LESSON_STEMS)
    school_hits = len(stems & _SCHOOL_STEMS)
    group_hits = len(stems & _GROUP_STEMS)
    anchor_penalty = 8 if anchor_stems and anchor_overlap <= 0 else 0
    singleton_penalty = 6 if word_count == 1 and not specific_stems else 0
    audience_only_penalty = 5 if group_hits and not (teacher_hits or lesson_hits or school_hits) else 0
    self_referential_penalty = 12 if stems & _SELF_REFERENTIAL_QUERY_STEMS else 0
    return (
        anchor_overlap * 18
        + len(specific_stems) * 8
        + word_count * 2
        + teacher_hits * 3
        + lesson_hits * 5
        + school_hits * 4
        - len(generic_stems) * 3
        - anchor_penalty
        - singleton_penalty
        - audience_only_penalty
        - self_referential_penalty
    )


def _query_category_tags(stems: set[str]) -> set[str]:
    tags: set[str] = set()
    if stems & _TEACHER_STEMS:
        tags.add("teacher")
    if stems & _LESSON_STEMS:
        tags.add("lesson")
    if stems & _SCHOOL_STEMS:
        tags.add("school")
    if stems & _GROUP_STEMS:
        tags.add("group")
    return tags


def _query_category_gain(tags: set[str], covered_tags: set[str]) -> int:
    gain = 0
    if "lesson" in tags and "lesson" not in covered_tags:
        gain += 4
    if "school" in tags and "school" not in covered_tags:
        gain += 3
    if "teacher" in tags and "teacher" not in covered_tags:
        gain += 2
    if "group" in tags and "group" not in covered_tags:
        gain += 1
    return gain


def _compact_query_variants(query: str, *, anchor_stems: set[str]) -> list[str]:
    tokens = [_normalize_token(token) for token in _TOKEN_RE.findall(str(query or ""))]
    if any(re.search(r"[а-я]", token) for token in tokens):
        return []
    filtered: list[str] = []
    for token in tokens:
        if len(token) < 3 or token in _QUERY_COMPRESSION_STOPWORDS:
            continue
        filtered.append(token)
    while filtered and _stem_token(filtered[0]) in _QUERY_LEAD_NOISE_STEMS:
        filtered.pop(0)
    if len(filtered) < 2:
        return []

    windows: list[str] = []
    fallback_windows: list[str] = []
    max_size = min(3, len(filtered))
    for size in range(max_size, 1, -1):
        for start in range(0, len(filtered) - size + 1):
            window = filtered[start : start + size]
            stems = _theme_token_stems(" ".join(window))
            specific = {stem for stem in stems if stem not in _GENERIC_DISCOVERY_STEMS}
            phrase = " ".join(window).strip()
            if len(phrase) > 48 or any(len(word) > 14 for word in phrase.split()):
                continue
            fallback_windows.append(phrase)
            if anchor_stems and not (specific & anchor_stems):
                continue
            windows.append(phrase)
        if windows:
            break
    if not windows:
        windows = fallback_windows
    seen: set[str] = set()
    out: list[str] = []
    for phrase in windows:
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(phrase)
        if len(out) >= 2:
            break
    return out


def _search_queries(keywords: list[str], *, max_queries: int = 6) -> list[str]:
    if max_queries <= 0:
        return []
    anchor_stems = _theme_anchor_stems(keywords)
    seen: set[str] = set()
    scored_queries: list[tuple[int, int, str, set[str], set[str]]] = []
    source_queries = list(keywords or [])
    source_queries.extend(_synthetic_search_queries(keywords, anchor_stems=anchor_stems))
    compact_queries: list[str] = []
    for raw in keywords or []:
        compact_queries.extend(_compact_query_variants(str(raw or ""), anchor_stems=anchor_stems))
    source_queries.extend(compact_queries)
    for index, raw in enumerate(source_queries):
        query = " ".join(str(raw or "").split()).strip()
        if len(query) < 3:
            continue
        words = [part for part in query.split() if part]
        if len(words) > 4 or len(query) > 48 or any(len(word) > 14 for word in words):
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        full_stems = _theme_token_stems(query)
        filtered_specific_stems = {stem for stem in full_stems if stem not in _GENERIC_DISCOVERY_STEMS}
        coverage_stems = full_stems
        if _is_identity_like_query(
            query,
            full_stems=full_stems,
            specific_stems=filtered_specific_stems,
            anchor_stems=anchor_stems,
        ):
            continue
        if full_stems & _SELF_REFERENTIAL_QUERY_STEMS and len(filtered_specific_stems & anchor_stems) <= 1:
            continue
        score = _query_utility_score(query, anchor_stems=anchor_stems)
        scored_queries.append((
            score,
            index,
            query,
            coverage_stems,
            _query_category_tags(coverage_stems),
        ))
    remaining = sorted(scored_queries, key=lambda item: (-item[0], item[1], item[2]))
    selected: list[str] = []
    covered_stems: set[str] = set()
    covered_tags: set[str] = set()
    if remaining:
        score, index, query, stems, tags = remaining.pop(0)
        selected.append(query)
        covered_stems |= stems
        covered_tags |= tags
    while remaining and len(selected) < max_queries:
        best_idx = 0
        best_value: tuple[int, int, int, int, int, str] | None = None
        for idx, (score, index, query, stems, tags) in enumerate(remaining):
            new_stems = len(stems - covered_stems)
            overlap = len(stems & covered_stems)
            category_gain = _query_category_gain(tags, covered_tags)
            value = (category_gain, new_stems, score, -overlap, -index, query)
            if best_value is None or value > best_value:
                best_idx = idx
                best_value = value
        score, index, query, stems, tags = remaining.pop(best_idx)
        selected.append(query)
        covered_stems |= stems
        covered_tags |= tags
    return selected


def _subject_core_phrase(query: str, *, anchor_stems: set[str]) -> str:
    kept: list[str] = []
    for token in _TOKEN_RE.findall(str(query or "")):
        stem = _stem_token(token)
        if anchor_stems:
            if stem in anchor_stems:
                kept.append(_normalize_token(token))
            continue
        if stem and stem not in _GENERIC_DISCOVERY_STEMS:
            kept.append(_normalize_token(token))
    return " ".join(kept[:3]).strip()


def _generic_stem_hints(query: str, *, full_stems: set[str]) -> set[str]:
    hints = set(full_stems & (_TEACHER_STEMS | _LESSON_STEMS | _GROUP_STEMS | _SCHOOL_STEMS))
    lowered = _normalize_token(query)
    if any(marker in lowered for marker in ("teacher", "tutor", "репетитор", "преподав", "учител")):
        hints.add("teacher")
    if any(marker in lowered for marker in ("lesson", "lessons", "урок", "обуч", "курс")):
        hints.add("lesson")
    if any(marker in lowered for marker in ("group", "groups", "групп", "ученик", "студент")):
        hints.add("group")
    if any(marker in lowered for marker in ("school", "academy", "школ")):
        hints.add("school")
    return hints


def _synthetic_search_queries(keywords: list[str], *, anchor_stems: set[str]) -> list[str]:
    if not keywords:
        return []
    subject_terms: list[str] = []
    generic_stems: set[str] = set()
    for raw in keywords:
        query = " ".join(str(raw or "").split()).strip()
        if not query:
            continue
        full_stems = _theme_token_stems(query)
        specific_stems = {stem for stem in full_stems if stem not in _GENERIC_DISCOVERY_STEMS}
        subject_core = _subject_core_phrase(query, anchor_stems=anchor_stems)
        if len(query.split()) != 1:
            if (
                subject_core
                and len(subject_core.split()) <= 3
                and (not anchor_stems or specific_stems & anchor_stems)
            ):
                subject_terms.append(subject_core)
            generic_stems |= _generic_stem_hints(query, full_stems=full_stems)
            continue
        if specific_stems and (not anchor_stems or specific_stems & anchor_stems):
            subject_terms.append(subject_core or query)
            continue
        generic_stems |= _generic_stem_hints(query, full_stems=full_stems)

    out: list[str] = []
    seen: set[str] = set()
    for subject in subject_terms[:2]:
        for variant in _subject_query_variants(subject=subject, generic_stems=generic_stems):
            key = variant.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(variant)
    return out


def _subject_query_variants(*, subject: str, generic_stems: set[str]) -> list[str]:
    normalized_subject = " ".join(str(subject or "").split()).strip()
    if not normalized_subject:
        return []
    if " " in normalized_subject:
        return []
    if re.fullmatch(r"[A-Za-z][A-Za-z\\s-]*", normalized_subject):
        base = normalized_subject
        variants: list[str] = []
        if generic_stems & _TEACHER_STEMS:
            variants.append(f"{base} teacher")
            variants.append(f"{base} tutor")
        if generic_stems & _LESSON_STEMS:
            variants.append(f"{base} lessons")
        if generic_stems & _GROUP_STEMS and generic_stems & _TEACHER_STEMS:
            variants.append(f"{base} teacher groups")
        if generic_stems & _SCHOOL_STEMS:
            variants.append(f"{base} school")
            variants.append(f"online {base} school")
        return variants

    russian_object = _russian_subject_object_form(normalized_subject)
    variants = []
    if generic_stems & _TEACHER_STEMS:
        variants.append(f"репетитор {russian_object}")
        variants.append(f"преподаватель {russian_object}")
    if generic_stems & _LESSON_STEMS:
        variants.append(f"уроки {russian_object}")
    if generic_stems & _GROUP_STEMS and not (generic_stems & _TEACHER_STEMS):
        variants.append(f"группы {russian_object}")
    if generic_stems & _TEACHER_STEMS and generic_stems & _GROUP_STEMS:
        variants.append(f"группы преподавателей {russian_object}")
    if generic_stems & _SCHOOL_STEMS:
        variants.append(f"школа {russian_object}")
        variants.append(f"онлайн школа {russian_object}")
    return variants


def _russian_subject_object_form(term: str) -> str:
    value = str(term or "").strip().lower()
    if " " in value:
        parts = [part for part in value.split() if part]
        if len(parts) == 2 and parts[1] in {"язык", "языка"}:
            return f"{_russian_subject_object_form(parts[0])} языка"
        return value
    if value.endswith("ий"):
        return value[:-2] + "ого"
    if value.endswith("ый") or value.endswith("ой"):
        return value[:-2] + "ого"
    if value.endswith("ая"):
        return value[:-2] + "ой"
    if value.endswith("ое"):
        return value[:-2] + "ого"
    if value.endswith("а"):
        return value[:-1] + "ы"
    if value.endswith("я"):
        return value[:-1] + "и"
    return value


def _manual_competitor_queries(
    competitors: list[SeedResolution],
    *,
    max_queries: int,
    keywords: list[str],
) -> list[str]:
    if max_queries <= 0:
        return []
    keyword_stems = _token_stems(*keywords)
    seen: set[str] = set()
    queries: list[str] = []
    for competitor in competitors or []:
        raw_candidates = [
            str(competitor.title or "").strip(),
            str(competitor.description or "").strip(),
            str(competitor.handle or "").strip(),
        ]
        for raw in raw_candidates:
            query = " ".join(raw.split()).strip()
            if len(query) < 3:
                continue
            stems = _token_stems(query)
            if not stems:
                continue
            if keyword_stems and not (stems & keyword_stems):
                continue
            key = query.lower()
            if key in seen:
                continue
            seen.add(key)
            queries.append(query)
            if len(queries) >= max_queries:
                return queries
    return queries


def _discovery_queries(
    *,
    platform: str,
    keywords: list[str],
    competitors: list[SeedResolution],
    max_queries: int,
) -> list[str]:
    base_queries = _search_queries(keywords, max_queries=max_queries)
    extra_capacity = max(0, int(max_queries) - len(base_queries))
    competitor_queries = _manual_competitor_queries(
        competitors,
        max_queries=extra_capacity,
        keywords=base_queries or keywords,
    )
    return base_queries + competitor_queries


def _competitor_hint_stems(competitors: list[SeedResolution]) -> set[str]:
    stems: set[str] = set()
    for competitor in competitors or []:
        stems |= _token_stems(competitor.title, competitor.description, competitor.handle)
    return stems


def _progressive_queries(platform: str, keywords: list[str]) -> list[str]:
    return _search_queries(keywords, max_queries=_DISCOVERY_QUERY_BUDGET.get(platform, 2))


def build_search_ready_keywords(*, keywords: list[str], max_keywords: int = 8) -> list[str]:
    return _search_queries(keywords, max_queries=max_keywords)


def _should_stop_discovery(*, platform: str, query_index: int, unique_candidates: int, max_candidates: int) -> bool:
    return False


def _get_tiktok_client() -> ApifyTikTokClient:
    config = get_tiktok_apify_config()
    profile_actor_id = str(getattr(config, "profile_actor_id", getattr(config, "actor_id", "")) or "")
    search_actor_id = str(getattr(config, "search_actor_id", "") or "")
    if config.provider != "apify":
        raise PlatformOnboardingError(f"Unsupported TikTok provider: {config.provider}")
    if not config.access_token:
        raise PlatformOnboardingError("TIKTOK_PROVIDER_ACCESS_TOKEN is not set")
    if not profile_actor_id:
        raise PlatformOnboardingError("TIKTOK_APIFY_PROFILE_ACTOR_ID is not set")
    if not search_actor_id:
        raise PlatformOnboardingError("TIKTOK_APIFY_SEARCH_ACTOR_ID is not set")
    if not config.base_url:
        raise PlatformOnboardingError("TIKTOK_PROVIDER_BASE_URL is not set")
    return ApifyTikTokClient(
        access_token=config.access_token,
        actor_id=profile_actor_id,
        search_actor_id=search_actor_id,
        base_url=config.base_url,
    )


def _get_instagram_client() -> ApifyInstagramClient:
    config = get_instagram_apify_config()
    profile_actor_id = str(getattr(config, "profile_actor_id", getattr(config, "actor_id", "")) or "")
    search_actor_id = str(getattr(config, "search_actor_id", "") or "")
    if config.provider != "apify":
        raise PlatformOnboardingError(f"Unsupported Instagram provider: {config.provider}")
    if not config.access_token:
        raise PlatformOnboardingError("INSTAGRAM_PROVIDER_ACCESS_TOKEN is not set")
    if not profile_actor_id:
        raise PlatformOnboardingError("INSTAGRAM_APIFY_PROFILE_ACTOR_ID is not set")
    if not search_actor_id:
        raise PlatformOnboardingError("INSTAGRAM_APIFY_SEARCH_ACTOR_ID is not set")
    if not config.base_url:
        raise PlatformOnboardingError("INSTAGRAM_PROVIDER_BASE_URL is not set")
    return ApifyInstagramClient(
        access_token=config.access_token,
        actor_id=profile_actor_id,
        search_actor_id=search_actor_id,
        base_url=config.base_url,
    )


def _get_tiktok_handle(seed: SeedResolution) -> str:
    handle = str(seed.handle or "").strip()
    if handle:
        return handle
    if seed.url and "/@" in seed.url:
        return seed.url.rstrip("/").split("/@")[-1].split("/", 1)[0].strip()
    raise PlatformOnboardingError("TikTok seed has no resolvable handle for analysis")


def _get_instagram_lookup(seed: SeedResolution) -> str:
    if seed.url:
        return seed.url.strip()
    if seed.handle:
        return seed.handle.strip()
    if seed.external_id:
        return seed.external_id.strip()
    raise PlatformOnboardingError("Instagram seed has no resolvable lookup value for analysis")


def _instagram_profile_texts(profile: dict, *, n: int) -> list[str]:
    texts: list[str] = []
    for item in profile_to_video_details(profile):
        text = str(item.description or item.title or "").strip()
        if text:
            texts.append(text)
        if len(texts) >= n:
            return texts
    return texts


def _mark_candidate_collectible(
    candidate: _DiscoveryCandidate,
    *,
    item_count: int,
    max_views: int,
    recent_count: int = 0,
) -> None:
    candidate.metadata["collectible_count"] = max(1, int(item_count))
    candidate.metadata["collectible_views"] = max(0, int(max_views))
    candidate.metadata["recent_collectible_count"] = max(0, int(recent_count))


def _get_cached_collectible_texts(
    *,
    platform: str,
    external_id: str,
    handle: str | None,
    n: int,
    context: SetupRunContext | None = None,
) -> list[str] | None:
    cache_key = _recent_cache_key(platform, external_id, handle, n)
    if context is not None and cache_key in context.recent_content_cache:
        return list(context.recent_content_cache[cache_key])
    retry_cached, retry_found = get_cached_retry_value(_retry_cache_key(layer="collectible-texts", key=cache_key))
    if retry_found and isinstance(retry_cached, list):
        if context is not None:
            context.recent_content_cache[cache_key] = list(retry_cached)
        return list(retry_cached)
    return None


def _recent_collectible_cutoff() -> datetime:
    return datetime.now(UTC) - timedelta(days=_DISCOVERY_RECENT_WINDOW_DAYS)


def _recent_collectible_count(details: list[Any]) -> int:
    cutoff = _recent_collectible_cutoff()
    return sum(1 for item in details if getattr(item, "published_at", None) and item.published_at >= cutoff)


def _store_cached_collectible_texts(
    *,
    platform: str,
    external_id: str,
    handle: str | None,
    n: int,
    texts: list[str],
    context: SetupRunContext | None = None,
) -> list[str]:
    cache_key = _recent_cache_key(platform, external_id, handle, n)
    normalized = [str(text) for text in texts if str(text or "").strip()]
    if context is not None:
        context.recent_content_cache[cache_key] = list(normalized)
    store_retry_value(_retry_cache_key(layer="collectible-texts", key=cache_key), list(normalized))
    return normalized


def _fetch_recent_youtube_short_texts(
    *,
    candidate: _DiscoveryCandidate,
    n: int,
    context: SetupRunContext | None = None,
) -> list[str]:
    texts, _views, _recent_count = _fetch_recent_youtube_upload_signals(candidate=candidate, n=n, context=context)
    return texts


def _fetch_recent_youtube_upload_signals(
    *,
    candidate: _DiscoveryCandidate,
    n: int,
    context: SetupRunContext | None = None,
) -> tuple[list[str], list[int], int]:
    cached = _get_cached_collectible_signals(
        platform=Platform.YOUTUBE,
        external_id=candidate.external_id,
        handle=candidate.handle,
        n=n,
        context=context,
    )
    if cached is not None:
        return cached

    try:
        client = get_youtube_client()
    except YouTubeNotConfigured:
        return _store_cached_collectible_signals(
            platform=Platform.YOUTUBE,
            external_id=candidate.external_id,
            handle=candidate.handle,
            n=n,
            texts=[],
            views=[],
            recent_count=0,
            context=context,
        )
    try:
        try:
            items = client.channels_list(part="contentDetails", ids=[candidate.external_id])
            if not items:
                return _store_cached_collectible_signals(
                    platform=Platform.YOUTUBE,
                    external_id=candidate.external_id,
                    handle=candidate.handle,
                    n=n,
                    texts=[],
                    views=[],
                    recent_count=0,
                    context=context,
                )
            uploads = ((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
            if not uploads:
                return _store_cached_collectible_signals(
                    platform=Platform.YOUTUBE,
                    external_id=candidate.external_id,
                    handle=candidate.handle,
                    n=n,
                    texts=[],
                    views=[],
                    recent_count=0,
                    context=context,
                )
            playlist_items = client.playlist_items(
                playlist_id=str(uploads),
                max_results=max(_DISCOVERY_YOUTUBE_UPLOAD_SCAN_LIMIT, n * 5),
            )
            video_ids = playlist_items_to_video_ids(playlist_items)
            if not video_ids:
                return _store_cached_collectible_signals(
                    platform=Platform.YOUTUBE,
                    external_id=candidate.external_id,
                    handle=candidate.handle,
                    n=n,
                    texts=[],
                    views=[],
                    recent_count=0,
                    context=context,
                )
            video_items = client.videos_list(
                ids=video_ids[: max(_DISCOVERY_YOUTUBE_UPLOAD_SCAN_LIMIT, n * 5)],
                part="snippet,contentDetails,statistics",
            )
        except YouTubeApiError:
            return _store_cached_collectible_signals(
                platform=Platform.YOUTUBE,
                external_id=candidate.external_id,
                handle=candidate.handle,
                n=n,
                texts=[],
                views=[],
                recent_count=0,
                context=context,
            )
        upload_details = sorted(
            [
                detail
                for detail in video_items_to_details(video_items)
                if str(detail.title or "").strip()
            ],
            key=lambda item: item.published_at,
            reverse=True,
        )
        recent_count = _recent_collectible_count(upload_details)
        top_details = upload_details[:n]
        texts = [
            str(detail.title or "").strip()
            for detail in top_details
            if str(detail.title or "").strip()
        ]
        views = [int(detail.views or 0) for detail in top_details]
        return _store_cached_collectible_signals(
            platform=Platform.YOUTUBE,
            external_id=candidate.external_id,
            handle=candidate.handle,
            n=n,
            texts=texts,
            views=views,
            recent_count=recent_count,
            context=context,
        )
    finally:
        client.close()


def _fetch_recent_youtube_short_signals(
    *,
    candidate: _DiscoveryCandidate,
    n: int,
    context: SetupRunContext | None = None,
) -> tuple[list[str], list[int], int]:
    return _fetch_recent_youtube_upload_signals(candidate=candidate, n=n, context=context)


def _fetch_recent_instagram_reel_texts(
    *,
    candidate: _DiscoveryCandidate,
    n: int,
    context: SetupRunContext | None = None,
) -> tuple[list[str], list[int], int]:
    cached = _get_cached_collectible_signals(
        platform=Platform.INSTAGRAM,
        external_id=candidate.external_id,
        handle=candidate.handle,
        n=n,
        context=context,
    )
    if cached is not None:
        return cached

    lookup = str(candidate.url or candidate.handle or candidate.external_id or "").strip()
    profiles = fetch_instagram_profiles_cached(
        inputs=[lookup],
        context=context,
        purpose="candidate_validation",
    )
    if not profiles:
        return _store_cached_collectible_signals(
            platform=Platform.INSTAGRAM,
            external_id=candidate.external_id,
            handle=candidate.handle,
            n=n,
            texts=[],
            views=[],
            recent_count=0,
            context=context,
        )
    details = sorted(profile_to_video_details(profiles[0]), key=lambda item: item.published_at, reverse=True)
    top_details = details[:n]
    texts = [
        str(item.description or item.title or "").strip()
        for item in top_details
        if str(item.description or item.title or "").strip()
    ]
    views = [int(item.views or 0) for item in top_details]
    return _store_cached_collectible_signals(
        platform=Platform.INSTAGRAM,
        external_id=candidate.external_id,
        handle=candidate.handle,
        n=n,
        texts=texts,
        views=views,
        recent_count=_recent_collectible_count(details),
        context=context,
    )


def _fetch_recent_tiktok_texts(
    *,
    candidate: _DiscoveryCandidate,
    n: int,
    context: SetupRunContext | None = None,
) -> tuple[list[str], list[int], int]:
    cached = _get_cached_collectible_signals(
        platform=Platform.TIKTOK,
        external_id=candidate.external_id,
        handle=candidate.handle,
        n=n,
        context=context,
    )
    if cached is not None:
        return cached

    handle = str(candidate.handle or "").strip()
    items = fetch_tiktok_profile_feed_cached(
        handle=handle,
        results_per_page=max(1, n),
        context=context,
        purpose="candidate_validation",
    )
    details: list[Any] = []
    for item in items:
        try:
            details.append(item_to_video_details(item))
        except ValueError:
            continue
    details = sorted(
        [item for item in details if int(item.views or 0) > 0],
        key=lambda item: item.published_at,
        reverse=True,
    )
    top_details = details[:n]
    texts = [
        str(item.description or item.title or "").strip()
        for item in top_details
        if str(item.description or item.title or "").strip()
    ]
    views = [int(item.views or 0) for item in top_details]
    return _store_cached_collectible_signals(
        platform=Platform.TIKTOK,
        external_id=candidate.external_id,
        handle=candidate.handle,
        n=n,
        texts=texts,
        views=views,
        recent_count=_recent_collectible_count(details),
        context=context,
    )


def _batch_fetch_recent_instagram_reel_texts(
    *,
    candidates: list[_DiscoveryCandidate],
    n: int,
    context: SetupRunContext | None = None,
) -> dict[str, tuple[list[str], list[int], int]]:
    results: dict[str, tuple[list[str], list[int], int]] = {}
    missing: list[_DiscoveryCandidate] = []
    for candidate in candidates:
        cached = _get_cached_collectible_signals(
            platform=Platform.INSTAGRAM,
            external_id=candidate.external_id,
            handle=candidate.handle,
            n=n,
            context=context,
        )
        if cached is not None:
            results[candidate.external_id] = cached
            continue
        missing.append(candidate)
    if not missing:
        return results

    lookups = [str(candidate.url or candidate.handle or candidate.external_id or "").strip() for candidate in missing]
    profiles = fetch_instagram_profiles_cached(
        inputs=lookups,
        context=context,
        purpose="candidate_validation",
    )
    for candidate, lookup in zip(missing, lookups):
        profile = _find_instagram_profile_for_lookup(profiles, lookup)
        if not profile:
            results[candidate.external_id] = _store_cached_collectible_signals(
                platform=Platform.INSTAGRAM,
                external_id=candidate.external_id,
                handle=candidate.handle,
                n=n,
                texts=[],
                views=[],
                recent_count=0,
                context=context,
            )
            continue
        details = sorted(profile_to_video_details(profile), key=lambda item: item.published_at, reverse=True)
        top_details = details[:n]
        texts = [
            str(item.description or item.title or "").strip()
            for item in top_details
            if str(item.description or item.title or "").strip()
        ]
        results[candidate.external_id] = _store_cached_collectible_signals(
            platform=Platform.INSTAGRAM,
            external_id=candidate.external_id,
            handle=candidate.handle,
            n=n,
            texts=texts,
            views=[int(item.views or 0) for item in top_details],
            recent_count=_recent_collectible_count(details),
            context=context,
        )
    return results


def _batch_fetch_recent_tiktok_texts(
    *,
    candidates: list[_DiscoveryCandidate],
    n: int,
    context: SetupRunContext | None = None,
) -> dict[str, tuple[list[str], list[int], int]]:
    results: dict[str, tuple[list[str], list[int], int]] = {}
    missing: list[_DiscoveryCandidate] = []
    handles: list[str] = []
    for candidate in candidates:
        cached = _get_cached_collectible_signals(
            platform=Platform.TIKTOK,
            external_id=candidate.external_id,
            handle=candidate.handle,
            n=n,
            context=context,
        )
        if cached is not None:
            results[candidate.external_id] = cached
            continue
        missing.append(candidate)
        handles.append(str(candidate.handle or "").strip())
    if not missing:
        return results

    grouped_items = fetch_tiktok_profile_feeds_cached(
        handles=handles,
        results_per_page=max(1, n),
        context=context,
        purpose="candidate_validation",
    )
    for candidate in missing:
        candidate_handle = str(candidate.handle or "").strip()
        items = grouped_items.get(candidate_handle, [])
        details: list[Any] = []
        for item in items:
            try:
                details.append(item_to_video_details(item))
            except ValueError:
                continue
        details = sorted(
            [item for item in details if int(item.views or 0) > 0],
            key=lambda item: item.published_at,
            reverse=True,
        )
        top_details = details[:n]
        texts = [
            str(item.description or item.title or "").strip()
            for item in top_details
            if str(item.description or item.title or "").strip()
        ]
        results[candidate.external_id] = _store_cached_collectible_signals(
            platform=Platform.TIKTOK,
            external_id=candidate.external_id,
            handle=candidate.handle,
            n=n,
            texts=texts,
            views=[int(item.views or 0) for item in top_details],
            recent_count=_recent_collectible_count(details),
            context=context,
        )
    return results


def _fetch_candidate_collectible_texts(
    *,
    candidate: _DiscoveryCandidate,
    context: SetupRunContext | None = None,
) -> tuple[list[str], list[int], int]:
    if candidate.platform == Platform.YOUTUBE:
        return _fetch_recent_youtube_short_signals(candidate=candidate, n=_DISCOVERY_VALIDATION_ITEMS, context=context)
    if candidate.platform == Platform.INSTAGRAM:
        return _fetch_recent_instagram_reel_texts(candidate=candidate, n=_DISCOVERY_VALIDATION_ITEMS, context=context)
    if candidate.platform == Platform.TIKTOK:
        return _fetch_recent_tiktok_texts(candidate=candidate, n=_DISCOVERY_VALIDATION_ITEMS, context=context)
    return [], [], 0


def _validate_instagram_candidate_collectible(
    *,
    candidate: _DiscoveryCandidate,
    context: SetupRunContext | None = None,
) -> bool:
    texts, views, recent_count = _fetch_recent_instagram_reel_texts(candidate=candidate, n=3, context=context)
    min_recent = _DISCOVERY_MIN_RECENT_ITEMS_BY_PLATFORM.get(Platform.INSTAGRAM, _DISCOVERY_MIN_RECENT_SHORTS)
    if not texts or recent_count < min_recent:
        return False
    _mark_candidate_collectible(
        candidate,
        item_count=len(texts),
        max_views=max(views) if views else 0,
        recent_count=recent_count,
    )
    return True


def _validate_tiktok_candidate_collectible(
    *,
    candidate: _DiscoveryCandidate,
    context: SetupRunContext | None = None,
) -> bool:
    texts, views, recent_count = _fetch_recent_tiktok_texts(candidate=candidate, n=3, context=context)
    min_recent = _DISCOVERY_MIN_RECENT_ITEMS_BY_PLATFORM.get(Platform.TIKTOK, _DISCOVERY_MIN_RECENT_SHORTS)
    if not texts or recent_count < min_recent:
        return False
    _mark_candidate_collectible(
        candidate,
        item_count=len(texts),
        max_views=max(views) if views else 0,
        recent_count=recent_count,
    )
    return True


def _validate_youtube_candidate_collectible(
    *,
    candidate: _DiscoveryCandidate,
    context: SetupRunContext | None = None,
) -> bool:
    texts, views, recent_count = _fetch_recent_youtube_short_signals(candidate=candidate, n=3, context=context)
    min_recent = _DISCOVERY_MIN_RECENT_ITEMS_BY_PLATFORM.get(Platform.YOUTUBE, _DISCOVERY_MIN_RECENT_SHORTS)
    if not texts or recent_count < min_recent:
        return False
    _mark_candidate_collectible(
        candidate,
        item_count=len(texts),
        max_views=max(views) if views else 0,
        recent_count=recent_count,
    )
    return True


def _collector_aware_candidates(
    *,
    platform: str,
    candidates: list[_DiscoveryCandidate],
    keywords: list[str],
    max_candidates: int,
    context: SetupRunContext | None = None,
) -> tuple[list[_DiscoveryCandidate], str]:
    if platform not in {Platform.YOUTUBE, Platform.INSTAGRAM, Platform.TIKTOK} or not candidates:
        return candidates, ""
    strong_profile_candidates: list[_DiscoveryCandidate] = []
    borderline_candidates: list[_DiscoveryCandidate] = []
    for candidate in candidates:
        if _theme_profile_passes(candidate=candidate, keywords=keywords):
            strong_profile_candidates.append(candidate)
            continue
        if len(candidate.query_hits) >= 1 or float(candidate.metadata.get("profile_theme_score") or 0) > 0:
            borderline_candidates.append(candidate)
    ranked_candidates = sorted(
        strong_profile_candidates + borderline_candidates,
        key=lambda item: (-_score_candidate(item), -len(item.query_hits), item.sort_tiebreak),
    )
    if not ranked_candidates:
        return [], "поиск выполнен, но кандидаты не совпали с темой ниши на уровне профиля."

    validated: list[_DiscoveryCandidate] = []
    failed_no_content = 0
    failed_low_activity = 0
    failed_offtopic = 0
    min_recent = _DISCOVERY_MIN_RECENT_ITEMS_BY_PLATFORM.get(platform, _DISCOVERY_MIN_RECENT_SHORTS)
    for candidate_batch in _progressive_validation_batches(platform=platform, ranked_candidates=ranked_candidates):
        batch_texts: dict[str, tuple[list[str], list[int], int]] = {}
        if platform == Platform.INSTAGRAM:
            batch_texts = _batch_fetch_recent_instagram_reel_texts(
                candidates=candidate_batch,
                n=_DISCOVERY_VALIDATION_ITEMS,
                context=context,
            )
        elif platform == Platform.TIKTOK:
            batch_texts = _batch_fetch_recent_tiktok_texts(
                candidates=candidate_batch,
                n=_DISCOVERY_VALIDATION_ITEMS,
                context=context,
            )
        for candidate in candidate_batch:
            if platform in {Platform.INSTAGRAM, Platform.TIKTOK}:
                texts, views, recent_count = batch_texts.get(candidate.external_id, ([], [], 0))
            else:
                texts, views, recent_count = _fetch_candidate_collectible_texts(candidate=candidate, context=context)
            if not texts:
                failed_no_content += 1
                continue
            if recent_count < min_recent:
                failed_low_activity += 1
                continue
            _mark_candidate_collectible(
                candidate,
                item_count=len(texts),
                max_views=max(views) if views else 0,
                recent_count=recent_count,
            )
            if not _theme_content_passes(
                platform=platform,
                candidate=candidate,
                texts=texts,
                keywords=keywords,
            ):
                failed_offtopic += 1
                continue
            validated.append(candidate)
            if len(validated) >= min(max_candidates, _DISCOVERY_EARLY_STOP_CANDIDATES.get(platform, max_candidates)):
                break
        if len(validated) >= min(max_candidates, _DISCOVERY_EARLY_STOP_CANDIDATES.get(platform, max_candidates)):
            break
    if validated:
        ranked = sorted(
            validated,
            key=lambda item: (-_score_candidate(item), -len(item.query_hits), item.sort_tiebreak),
        )
        return ranked[:max_candidates], ""

    if failed_low_activity >= max(failed_no_content, failed_offtopic):
        if platform == Platform.YOUTUBE:
            return [], "поиск выполнен, но топ-кандидаты не прошли фильтр активности: меньше 2 recent видео за 60 дней."
        if platform == Platform.INSTAGRAM:
            return [], "поиск выполнен, но топ-кандидаты не прошли фильтр активности: нет recent Reels за 60 дней."
        return [], "поиск выполнен, но топ-кандидаты не прошли фильтр активности: меньше 2 recent TikTok-видео за 60 дней."
    if platform == Platform.YOUTUBE:
        return [], "поиск выполнен, но топ-кандидаты не прошли проверку recent видео по теме."
    if platform == Platform.INSTAGRAM:
        return [], "поиск выполнен, но топ-кандидаты не прошли проверку reels по теме."
    return [], "поиск выполнен, но топ-кандидаты не прошли проверку recent TikTok-видео по теме."


def _balanced_validation_candidates(
    candidates: list[_DiscoveryCandidate],
    *,
    budget: int,
    excluded_ids: set[str] | None = None,
) -> list[_DiscoveryCandidate]:
    excluded = set(excluded_ids or set())
    remaining_candidates = [candidate for candidate in candidates if candidate.external_id not in excluded]
    if budget >= len(remaining_candidates):
        return list(remaining_candidates)

    buckets: dict[str, list[_DiscoveryCandidate]] = {}
    for candidate in remaining_candidates:
        for query in sorted(candidate.query_hits):
            buckets.setdefault(query, []).append(candidate)

    ordered_queries = sorted(
        buckets,
        key=lambda query: (-len(buckets[query]), query),
    )
    selected: list[_DiscoveryCandidate] = []
    seen_ids: set[str] = set()
    while len(selected) < budget:
        progress = False
        for query in ordered_queries:
            bucket = buckets.get(query) or []
            while bucket:
                candidate = bucket.pop(0)
                if candidate.external_id in seen_ids:
                    continue
                selected.append(candidate)
                seen_ids.add(candidate.external_id)
                progress = True
                break
            if len(selected) >= budget:
                break
        if not progress:
            break

    if len(selected) >= budget:
        return selected[:budget]

    for candidate in remaining_candidates:
        if candidate.external_id in seen_ids:
            continue
        selected.append(candidate)
        if len(selected) >= budget:
            break
    return selected[:budget]


def _progressive_validation_batches(
    *,
    platform: str,
    ranked_candidates: list[_DiscoveryCandidate],
) -> list[list[_DiscoveryCandidate]]:
    if not ranked_candidates:
        return []
    batch_size = max(1, _DISCOVERY_VALIDATION_BUDGET.get(platform, 1))
    max_scan = max(batch_size, _DISCOVERY_VALIDATION_MAX_SCAN.get(platform, batch_size))
    batches: list[list[_DiscoveryCandidate]] = []
    seen_ids: set[str] = set()
    scanned = 0
    while scanned < min(max_scan, len(ranked_candidates)):
        remaining = [candidate for candidate in ranked_candidates if candidate.external_id not in seen_ids]
        if not remaining:
            break
        batch = _balanced_validation_candidates(
            remaining,
            budget=min(batch_size, max_scan - scanned, len(remaining)),
            excluded_ids=seen_ids,
        )
        if not batch:
            break
        batches.append(batch)
        seen_ids.update(candidate.external_id for candidate in batch)
        scanned += len(batch)
        if len(batch) < batch_size:
            break
    return batches


def get_recent_seed_content_texts(
    *,
    seed: SeedResolution,
    n: int = 10,
    context: SetupRunContext | None = None,
) -> list[str]:
    cache_key = _recent_cache_key(seed.platform, seed.external_id, seed.handle, n)
    if context is not None and cache_key in context.recent_content_cache:
        return list(context.recent_content_cache[cache_key])
    retry_cached, retry_found = get_cached_retry_value(_retry_cache_key(layer="recent-content", key=cache_key))
    if retry_found and isinstance(retry_cached, list):
        if context is not None:
            context.recent_content_cache[cache_key] = list(retry_cached)
        return list(retry_cached)

    if seed.platform == Platform.YOUTUBE:
        texts = get_recent_video_titles(seed, n=n)
        if context is not None:
            context.recent_content_cache[cache_key] = list(texts)
        store_retry_value(
            _retry_cache_key(layer="recent-content", key=cache_key),
            list(texts),
            ttl_seconds=int(getattr(settings, "YOUTUBE_RECENT_CACHE_TTL_SECONDS", 3600) or 3600),
        )
        return texts

    if seed.platform == Platform.TIKTOK:
        items = fetch_tiktok_profile_feed_cached(
            handle=_get_tiktok_handle(seed),
            results_per_page=max(1, n),
            context=context,
            purpose="keyword_recent_content",
        )
        texts: list[str] = []
        for item in items[:n]:
            try:
                details = item_to_video_details(item)
            except ValueError:
                continue
            text = details.description or details.title
            if text:
                texts.append(text)
        if context is not None:
            context.recent_content_cache[cache_key] = list(texts)
        store_retry_value(_retry_cache_key(layer="recent-content", key=cache_key), list(texts))
        return texts

    if seed.platform == Platform.INSTAGRAM:
        profiles = fetch_instagram_profiles_cached(
            inputs=[_get_instagram_lookup(seed)],
            context=context,
            purpose="keyword_recent_content",
        )
        if not profiles:
            if context is not None:
                context.recent_content_cache[cache_key] = []
            store_retry_value(_retry_cache_key(layer="recent-content", key=cache_key), [])
            return []
        texts = _instagram_profile_texts(profiles[0], n=n)
        if context is not None:
            context.recent_content_cache[cache_key] = list(texts)
        store_retry_value(_retry_cache_key(layer="recent-content", key=cache_key), list(texts))
        return texts

    return []


def _build_youtube_candidate(item: dict[str, Any], *, queries: set[str]) -> _DiscoveryCandidate | None:
    external_id = str(item.get("id") or "").strip()
    if not external_id:
        return None
    snippet = item.get("snippet") or {}
    statistics = item.get("statistics") or {}
    custom_url = str(snippet.get("customUrl") or "").strip()
    handle = custom_url[1:] if custom_url.startswith("@") else None
    return _DiscoveryCandidate(
        platform=Platform.YOUTUBE,
        external_id=external_id,
        handle=handle,
        url=f"https://www.youtube.com/channel/{external_id}",
        display_name=str(snippet.get("title") or "").strip() or None,
        description=str(snippet.get("description") or "").strip(),
        query_hits=set(queries),
        metadata={"rank_hint": int(statistics.get("subscriberCount") or 0)},
    )


def _discover_youtube_search_candidates(
    *,
    keywords: list[str],
    competitors: list[SeedResolution],
    max_search_calls: int,
    context: SetupRunContext | None = None,
) -> list[_DiscoveryCandidate]:
    initial_budget = max(1, int(max_search_calls))
    queries = _discovery_queries(
        platform=Platform.YOUTUBE,
        keywords=keywords,
        competitors=competitors,
        max_queries=initial_budget + 3,
    )
    if not queries:
        raise PlatformOnboardingError("No YouTube search queries could be built from niche keywords")
    base_queries = list(queries)
    query_hits: dict[str, set[str]] = {}
    expanded_fallback = False
    index = 0
    while index < len(queries):
        query = queries[index]
        if index >= initial_budget and len(query_hits) >= _DISCOVERY_EARLY_STOP_CANDIDATES.get(Platform.YOUTUBE, 20):
            break
        for channel_id in _cached_youtube_search_channel_ids(
            query=query,
            max_results=_DISCOVERY_RESULT_BUDGET[Platform.YOUTUBE],
            context=context,
        ):
            query_hits.setdefault(channel_id, set()).add(query)
        if _should_stop_discovery(
            platform=Platform.YOUTUBE,
            query_index=index,
            unique_candidates=len(query_hits),
            max_candidates=_DISCOVERY_EARLY_STOP_CANDIDATES.get(Platform.YOUTUBE, 3),
        ):
            break
        if (
            not expanded_fallback
            and index + 1 >= len(base_queries)
            and len(query_hits) < 12
        ):
            for extra_query in _youtube_fallback_queries(base_queries):
                if extra_query.lower() not in {item.lower() for item in queries}:
                    queries.append(extra_query)
            expanded_fallback = True
        index += 1
    if not query_hits:
        return []

    client = get_youtube_client()
    try:
        out: list[_DiscoveryCandidate] = []
        channel_ids = list(query_hits)
        for index in range(0, len(channel_ids), 50):
            items = client.channels_list(part="snippet,statistics", ids=channel_ids[index : index + 50])
            for item in items:
                channel_id = str(item.get("id") or "").strip()
                candidate = _build_youtube_candidate(item, queries=query_hits.get(channel_id, set()))
                if candidate:
                    out.append(candidate)
        return out
    finally:
        client.close()


def _youtube_fallback_queries(base_queries: list[str]) -> list[str]:
    lowered = [str(query or "").strip().lower() for query in base_queries if str(query or "").strip()]
    if not lowered:
        return []
    out: list[str] = []

    def add(query: str) -> None:
        normalized = " ".join(str(query or "").split()).strip()
        if not normalized:
            return
        key = normalized.lower()
        if key in lowered or key in {item.lower() for item in out}:
            return
        out.append(normalized)

    if any("англий" in query for query in lowered):
        if any("начинающ" in query for query in lowered):
            add("english for beginners")
        if any("урок" in query for query in lowered):
            add("english lessons")
        if any("репетитор" in query or "преподав" in query for query in lowered):
            add("english teacher")
        if any("школ" in query for query in lowered):
            add("online english school")
        if any("взросл" in query for query in lowered):
            add("english for adults")
        if any("разговорн" in query for query in lowered):
            add("spoken english")

    if any("dota" in query or "дота" in query for query in lowered):
        add("dota 2 guide")
        add("dota 2 tips")
        if any("патч" in query for query in lowered):
            add("dota 2 patch")

    return out[:4]


def _build_instagram_candidate(raw: dict[str, Any], *, queries: set[str]) -> _DiscoveryCandidate | None:
    external_id = str(raw.get("id") or "").strip()
    username = str(raw.get("username") or "").strip()
    if not external_id or not username:
        return None
    return _DiscoveryCandidate(
        platform=Platform.INSTAGRAM,
        external_id=external_id,
        handle=username,
        url=str(raw.get("url") or build_instagram_profile_url(username)),
        display_name=str(raw.get("full_name") or raw.get("fullName") or username).strip() or None,
        description=str(raw.get("biography") or "").strip(),
        query_hits=set(queries),
        metadata={"verified": bool(raw.get("is_verified") or raw.get("isVerified"))},
    )


def _build_instagram_related_candidate(raw: dict[str, Any], *, queries: set[str]) -> _DiscoveryCandidate | None:
    external_id = str(raw.get("id") or "").strip()
    username = str(raw.get("username") or "").strip()
    if not external_id or not username:
        return None
    return _DiscoveryCandidate(
        platform=Platform.INSTAGRAM,
        external_id=external_id,
        handle=username,
        url=build_instagram_profile_url(username),
        display_name=str(raw.get("full_name") or raw.get("fullName") or username).strip() or None,
        description="",
        query_hits=set(queries),
        metadata={
            "verified": bool(raw.get("is_verified") or raw.get("isVerified")),
            "source": "related_profile",
            "graph_depth": 1,
            "graph_hits": 1,
        },
    )


def _expand_instagram_related_candidates(
    *,
    candidates: list[_DiscoveryCandidate],
    context: SetupRunContext | None = None,
) -> list[_DiscoveryCandidate]:
    if not candidates:
        return []
    merged: dict[str, _DiscoveryCandidate] = {candidate.external_id: candidate for candidate in candidates}
    expanded_ids: set[str] = set()
    frontier = list(candidates)
    for depth in range(1, _INSTAGRAM_GRAPH_EXPANSION_DEPTH + 1):
        ranked = sorted(
            [candidate for candidate in frontier if candidate.external_id not in expanded_ids],
            key=lambda item: (-_score_candidate(item), -len(item.query_hits), item.sort_tiebreak),
        )
        expansion_candidates = ranked[:_INSTAGRAM_RELATED_EXPANSION_PROFILES]
        if not expansion_candidates:
            break
        expanded_ids.update(candidate.external_id for candidate in expansion_candidates)
        lookups = [str(candidate.url or candidate.handle or candidate.external_id or "").strip() for candidate in expansion_candidates]
        try:
            profiles = fetch_instagram_profiles_cached(
                inputs=lookups,
                context=context,
                purpose="candidate_validation",
            )
        except (PlatformOnboardingError, InstagramApiError, RuntimeError):
            break
        next_frontier: list[_DiscoveryCandidate] = []
        for candidate, lookup in zip(expansion_candidates, lookups):
            profile = _find_instagram_profile_for_lookup(profiles, lookup)
            if not profile:
                continue
            biography = str(profile.get("biography") or "").strip()
            if biography and not str(candidate.description or "").strip():
                candidate.description = biography
            related_items = profile.get("relatedProfiles") or []
            if not isinstance(related_items, list):
                continue
            for raw_related in related_items[:_INSTAGRAM_RELATED_PER_PROFILE]:
                if not isinstance(raw_related, dict):
                    continue
                related = _build_instagram_related_candidate(raw_related, queries=candidate.query_hits)
                if not related:
                    continue
                related.metadata["graph_depth"] = max(1, depth)
                related.metadata["graph_hits"] = 1
                existing = merged.get(related.external_id)
                if existing is None:
                    merged[related.external_id] = related
                    next_frontier.append(related)
                    continue
                existing.query_hits.update(related.query_hits)
                existing.metadata.update({k: v for k, v in related.metadata.items() if k not in {"graph_hits", "graph_depth"}})
                existing.metadata["graph_hits"] = int(existing.metadata.get("graph_hits") or 0) + 1
                current_depth = int(existing.metadata.get("graph_depth") or 0)
                existing.metadata["graph_depth"] = min(current_depth, depth) if current_depth else depth
                if not existing.display_name and related.display_name:
                    existing.display_name = related.display_name
                if not existing.handle and related.handle:
                    existing.handle = related.handle
                if not existing.url and related.url:
                    existing.url = related.url
        frontier = next_frontier
    return list(merged.values())


def _instagram_seed_accounts(*, seed_accounts: list[SeedResolution] | None) -> list[SeedResolution]:
    out: list[SeedResolution] = []
    seen: set[str] = set()
    for account in seed_accounts or []:
        if not account or account.platform != Platform.INSTAGRAM:
            continue
        key = str(account.external_id or account.handle or account.url or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(account)
    return out


def _seed_instagram_related_candidates(
    *,
    seed_accounts: list[SeedResolution] | None,
    context: SetupRunContext | None = None,
) -> list[_DiscoveryCandidate]:
    instagram_accounts = _instagram_seed_accounts(seed_accounts=seed_accounts)
    if not instagram_accounts:
        return []
    lookups = [str(account.url or account.handle or account.external_id or "").strip() for account in instagram_accounts]
    try:
        profiles = fetch_instagram_profiles_cached(
            inputs=lookups,
            context=context,
            purpose="keyword_recent_content",
        )
    except (PlatformOnboardingError, InstagramApiError, RuntimeError):
        return []

    out: dict[str, _DiscoveryCandidate] = {}
    for account, lookup in zip(instagram_accounts, lookups):
        profile = _find_instagram_profile_for_lookup(profiles, lookup)
        if not profile:
            continue
        related_items = profile.get("relatedProfiles") or []
        if not isinstance(related_items, list):
            continue
        for raw_related in related_items[:_INSTAGRAM_RELATED_PER_PROFILE]:
            if not isinstance(raw_related, dict):
                continue
            candidate = _build_instagram_related_candidate(raw_related, queries={"seed_related"})
            if not candidate:
                continue
            candidate.metadata["graph_depth"] = 1
            candidate.metadata["graph_hits"] = 1
            existing = out.get(candidate.external_id)
            if existing is None:
                out[candidate.external_id] = candidate
                continue
            existing.query_hits.update(candidate.query_hits)
            existing.metadata.update({k: v for k, v in candidate.metadata.items() if k not in {"graph_hits", "graph_depth"}})
            existing.metadata["graph_hits"] = int(existing.metadata.get("graph_hits") or 0) + 1
            current_depth = int(existing.metadata.get("graph_depth") or 0)
            existing.metadata["graph_depth"] = min(current_depth, 1) if current_depth else 1
    return list(out.values())


def _search_instagram_candidates_raw(
    *,
    keywords: list[str],
    competitors: list[SeedResolution],
    seed_accounts: list[SeedResolution] | None = None,
    max_candidates: int = 20,
    context: SetupRunContext | None = None,
) -> list[_DiscoveryCandidate]:
    queries = _discovery_queries(
        platform=Platform.INSTAGRAM,
        keywords=keywords,
        competitors=competitors,
        max_queries=_DISCOVERY_QUERY_BUDGET.get(Platform.INSTAGRAM, 2),
    )
    raw_by_id: dict[str, _DiscoveryCandidate] = {}
    for candidate in _seed_instagram_related_candidates(seed_accounts=seed_accounts, context=context):
        raw_by_id[candidate.external_id] = candidate
    if not queries and raw_by_id:
        raw_pool = _expand_instagram_related_candidates(
            candidates=list(raw_by_id.values()),
            context=context,
        )
        return sorted(
            raw_pool,
            key=lambda item: (-_score_candidate(item), -len(item.query_hits), item.sort_tiebreak),
        )
    if not queries:
        raise PlatformOnboardingError("No Instagram search queries could be built from niche keywords")
    result_limit = max(max_candidates, _DISCOVERY_RESULT_BUDGET[Platform.INSTAGRAM])
    for index, query in enumerate(queries):
        for raw in _cached_instagram_search_results(
            query=query,
            limit=result_limit,
            context=context,
            purpose="discovery_search",
        ):
            candidate = _build_instagram_candidate(raw, queries={query})
            if not candidate:
                continue
            existing = raw_by_id.get(candidate.external_id)
            if existing is None:
                raw_by_id[candidate.external_id] = candidate
            else:
                existing.query_hits.update(candidate.query_hits)
        if _should_stop_discovery(
            platform=Platform.INSTAGRAM,
            query_index=index,
            unique_candidates=len(raw_by_id),
            max_candidates=max_candidates,
        ):
            break

    if not raw_by_id:
        return []

    raw_pool = _expand_instagram_related_candidates(
        candidates=list(raw_by_id.values()),
        context=context,
    )

    ranked = sorted(
        raw_pool,
        key=lambda item: (-_score_candidate(item), -len(item.query_hits), item.sort_tiebreak),
    )
    return ranked


def discover_instagram_competitors(
    *,
    keywords: list[str],
    competitors: list[SeedResolution] | None = None,
    max_candidates: int = 20,
    context: SetupRunContext | None = None,
) -> list[CompetitorCandidate]:
    return [
        CompetitorCandidate(
            platform=item.platform,
            external_id=item.external_id,
            handle=item.handle,
            url=item.url,
            display_name=item.display_name,
            reason="search: " + ", ".join(sorted(item.query_hits)),
        )
        for item in _search_instagram_candidates_raw(
            keywords=keywords,
            competitors=list(competitors or []),
            seed_accounts=[],
            max_candidates=max_candidates,
            context=context,
        )
        [:max_candidates]
    ]


def _build_tiktok_candidate(raw: dict[str, Any], *, queries: set[str]) -> _DiscoveryCandidate | None:
    external_id = str(raw.get("id") or "").strip()
    handle = str(raw.get("name") or "").strip()
    if not external_id or not handle:
        return None
    return _DiscoveryCandidate(
        platform=Platform.TIKTOK,
        external_id=external_id,
        handle=handle,
        url=str(raw.get("profileUrl") or build_tiktok_profile_url(handle)),
        display_name=str(raw.get("nickName") or handle).strip() or None,
        description=str(raw.get("signature") or "").strip(),
        query_hits=set(queries),
        metadata={
            "verified": bool(raw.get("verified")),
            "rank_hint": int(raw.get("fans") or raw.get("heart") or 0),
        },
    )


def _search_tiktok_candidates_raw(
    *,
    keywords: list[str],
    competitors: list[SeedResolution],
    max_candidates: int = 20,
    context: SetupRunContext | None = None,
) -> list[_DiscoveryCandidate]:
    queries = _discovery_queries(
        platform=Platform.TIKTOK,
        keywords=keywords,
        competitors=competitors,
        max_queries=_DISCOVERY_QUERY_BUDGET.get(Platform.TIKTOK, 2),
    )
    if not queries:
        raise PlatformOnboardingError("No TikTok search queries could be built from niche keywords")
    raw_by_id: dict[str, _DiscoveryCandidate] = {}
    result_limit = min(max_candidates, _DISCOVERY_RESULT_BUDGET[Platform.TIKTOK])
    for index, query in enumerate(queries):
        for raw in _cached_tiktok_search_results(
            query=query,
            limit=result_limit,
            context=context,
            purpose="discovery_search",
        ):
            candidate = _build_tiktok_candidate(raw, queries={query})
            if not candidate:
                continue
            existing = raw_by_id.get(candidate.external_id)
            if existing is None:
                raw_by_id[candidate.external_id] = candidate
            else:
                existing.query_hits.update(candidate.query_hits)
        if _should_stop_discovery(
            platform=Platform.TIKTOK,
            query_index=index,
            unique_candidates=len(raw_by_id),
            max_candidates=max_candidates,
        ):
            break

    ranked = sorted(
        raw_by_id.values(),
        key=lambda item: (-_score_candidate(item), -len(item.query_hits), item.sort_tiebreak),
    )
    return ranked


def discover_tiktok_competitors(
    *,
    keywords: list[str],
    competitors: list[SeedResolution] | None = None,
    max_candidates: int = 20,
    context: SetupRunContext | None = None,
) -> list[CompetitorCandidate]:
    return [
        CompetitorCandidate(
            platform=item.platform,
            external_id=item.external_id,
            handle=item.handle,
            url=item.url,
            display_name=item.display_name,
            reason="search: " + ", ".join(sorted(item.query_hits)),
        )
        for item in _search_tiktok_candidates_raw(
            keywords=keywords,
            competitors=list(competitors or []),
            max_candidates=max_candidates,
            context=context,
        )
        [:max_candidates]
    ]


def _apply_cross_platform_bonus(candidates_by_platform: dict[str, list[_DiscoveryCandidate]]) -> None:
    identity_map: dict[str, set[str]] = {}
    for platform, candidates in candidates_by_platform.items():
        for candidate in candidates:
            identity_key = _candidate_identity_key(candidate)
            if not identity_key:
                continue
            identity_map.setdefault(identity_key, set()).add(platform)

    for platform, candidates in candidates_by_platform.items():
        for candidate in candidates:
            identity_key = _candidate_identity_key(candidate)
            seen_platforms = identity_map.get(identity_key, set())
            if len(seen_platforms) > 1:
                candidate.cross_platform_keys.update(seen_platforms - {platform})


def _filter_and_rank_candidates(
    *,
    candidates: list[_DiscoveryCandidate],
    seed: SeedResolution | None,
    linked_accounts: list[SeedResolution] | None,
    competitors: list[SeedResolution],
    max_candidates: int,
) -> list[CompetitorCandidate]:
    excluded_ids, handles_by_platform, seed_titles = _identity_keys(seed, linked_accounts)
    competitor_stems = _competitor_hint_stems(competitors)
    filtered: list[_DiscoveryCandidate] = []
    for candidate in candidates:
        if _is_seed_like_candidate(
            candidate,
            excluded_ids=excluded_ids,
            handles_by_platform=handles_by_platform,
            seed_titles=seed_titles,
        ):
            continue
        if not candidate.query_hits:
            continue
        if competitor_stems:
            candidate_stems = _token_stems(candidate.handle, candidate.display_name, candidate.description)
            candidate.metadata["competitor_overlap"] = len(candidate_stems & competitor_stems)
        filtered.append(candidate)

    ranked = sorted(
        filtered,
        key=lambda item: (-_score_candidate(item), -len(item.query_hits), item.sort_tiebreak),
    )
    return [
        CompetitorCandidate(
            platform=item.platform,
            external_id=item.external_id,
            handle=item.handle,
            url=item.url,
            display_name=item.display_name,
            reason="search: " + ", ".join(sorted(item.query_hits)),
        )
        for item in ranked[:max_candidates]
    ]


def discover_competitors_for_onboarding(
    *,
    keywords: list[str],
    seed: SeedResolution | None,
    competitors: list[SeedResolution],
    linked_accounts: list[SeedResolution] | None = None,
    max_youtube_search_calls: int,
    max_candidates_per_platform: int,
    context: SetupRunContext | None = None,
) -> DiscoveryOutcome:
    platform_statuses: list[PlatformDiscoveryStatus] = []
    candidates_by_platform: dict[str, list[_DiscoveryCandidate]] = {
        Platform.YOUTUBE: [],
        Platform.TIKTOK: [],
        Platform.INSTAGRAM: [],
    }

    for platform in (Platform.YOUTUBE, Platform.INSTAGRAM, Platform.TIKTOK):
        if platform_is_blocked(context, platform):
            state = get_platform_state(context, platform)
            platform_statuses.append(
                PlatformDiscoveryStatus(
                    platform=platform,
                    status=state.state,
                    reason=state.reason or f"{_platform_label(platform)} недоступен для этого setup.",
                )
            )

    if not any(status.platform == Platform.YOUTUBE for status in platform_statuses):
        if max_youtube_search_calls <= 0:
            reason = "YT_MAX_SEARCH_CALLS_PER_SETUP=0; поиск YouTube отключен для этого setup."
            mark_platform_skipped(context, Platform.YOUTUBE, reason=reason)
            platform_statuses.append(
                PlatformDiscoveryStatus(platform=Platform.YOUTUBE, status=DISCOVERY_SKIPPED, reason=reason)
            )
        else:
            try:
                candidates_by_platform[Platform.YOUTUBE] = _discover_youtube_search_candidates(
                    keywords=keywords,
                    competitors=competitors,
                    max_search_calls=min(max_youtube_search_calls, _DISCOVERY_QUERY_BUDGET[Platform.YOUTUBE]),
                    context=context,
                )
            except (PlatformOnboardingError, YouTubeApiError, RuntimeError) as exc:
                platform_statuses.append(
                    PlatformDiscoveryStatus(platform=Platform.YOUTUBE, status=DISCOVERY_ERROR, reason=str(exc))
                )
                mark_platform_failure(context, platform=Platform.YOUTUBE, reason=str(exc))

    if not any(status.platform == Platform.INSTAGRAM for status in platform_statuses):
        try:
            candidates_by_platform[Platform.INSTAGRAM] = _search_instagram_candidates_raw(
                keywords=keywords,
                competitors=competitors,
                seed_accounts=([seed] if seed else []) + list(linked_accounts or []),
                max_candidates=max_candidates_per_platform,
                context=context,
            )
        except (PlatformOnboardingError, InstagramApiError, RuntimeError) as exc:
            state = mark_platform_failure(context, platform=Platform.INSTAGRAM, reason=str(exc))
            platform_statuses.append(
                PlatformDiscoveryStatus(platform=Platform.INSTAGRAM, status=state, reason=str(exc))
            )

    if not any(status.platform == Platform.TIKTOK for status in platform_statuses):
        try:
            candidates_by_platform[Platform.TIKTOK] = _search_tiktok_candidates_raw(
                keywords=keywords,
                competitors=competitors,
                max_candidates=max_candidates_per_platform,
                context=context,
            )
        except (PlatformOnboardingError, TikTokApiError, RuntimeError) as exc:
            state = mark_platform_failure(context, platform=Platform.TIKTOK, reason=str(exc))
            platform_statuses.append(
                PlatformDiscoveryStatus(platform=Platform.TIKTOK, status=state, reason=str(exc))
            )

    _apply_cross_platform_bonus(candidates_by_platform)

    final_candidates: list[CompetitorCandidate] = []
    errored_platforms = {
        status.platform
        for status in platform_statuses
        if status.status in {DISCOVERY_ERROR, DISCOVERY_UNAVAILABLE, DISCOVERY_SKIPPED}
    }
    empty_reason_by_platform: dict[str, str] = {}
    for platform in (Platform.YOUTUBE, Platform.INSTAGRAM, Platform.TIKTOK):
        if platform in errored_platforms:
            continue
        if platform in {Platform.YOUTUBE, Platform.INSTAGRAM, Platform.TIKTOK}:
            try:
                validated_candidates, empty_reason = _collector_aware_candidates(
                    platform=platform,
                    candidates=candidates_by_platform[platform],
                    keywords=keywords,
                    max_candidates=max_candidates_per_platform,
                    context=context,
                )
            except (PlatformOnboardingError, InstagramApiError, TikTokApiError, RuntimeError) as exc:
                state = mark_platform_failure(context, platform=platform, reason=str(exc))
                platform_statuses.append(
                    PlatformDiscoveryStatus(platform=platform, status=state, reason=str(exc))
                )
                errored_platforms.add(platform)
                continue
            candidates_by_platform[platform] = validated_candidates
            if empty_reason:
                empty_reason_by_platform[platform] = empty_reason
            if (
                platform == Platform.YOUTUBE
                and len(validated_candidates) < _YOUTUBE_LOW_RECALL_RESCUE_THRESHOLD
            ):
                rescue_queries = [
                    query
                    for query in _youtube_fallback_queries(keywords)
                    if query.lower() not in {item.lower() for item in keywords}
                ]
                if rescue_queries:
                    rescue_raw = _discover_youtube_search_candidates(
                        keywords=keywords + rescue_queries,
                        competitors=competitors,
                        max_search_calls=max(len(keywords) + len(rescue_queries), _DISCOVERY_QUERY_BUDGET[Platform.YOUTUBE]),
                        context=context,
                    )
                    rescue_validated, rescue_reason = _collector_aware_candidates(
                        platform=platform,
                        candidates=_merge_discovery_candidates(candidates_by_platform[platform], rescue_raw),
                        keywords=keywords + rescue_queries,
                        max_candidates=max_candidates_per_platform,
                        context=context,
                    )
                    if rescue_validated:
                        candidates_by_platform[platform] = rescue_validated
                        empty_reason_by_platform.pop(platform, None)
                    elif rescue_reason:
                        empty_reason_by_platform[platform] = rescue_reason
        ranked = _filter_and_rank_candidates(
            candidates=candidates_by_platform[platform],
            seed=seed,
            linked_accounts=linked_accounts,
            competitors=competitors,
            max_candidates=max_candidates_per_platform,
        )
        final_candidates.extend(ranked)
        platform_statuses.append(
            PlatformDiscoveryStatus(
                platform=platform,
                status=DISCOVERY_FOUND if ranked else DISCOVERY_EMPTY,
                candidate_count=len(ranked),
                reason=(
                    ""
                    if ranked and len(ranked) >= max_candidates_per_platform
                    else (
                        f"найдено {len(ranked)} валидных кандидатов; после всех поисковых фраз и проверок больше подтвержденных профилей не осталось."
                        if ranked
                        else empty_reason_by_platform.get(
                            platform,
                            "по текущим поисковым фразам поиск был выполнен, но кандидаты не найдены.",
                        )
                    )
                ),
            )
        )

    platform_statuses.sort(key=lambda item: [Platform.YOUTUBE, Platform.INSTAGRAM, Platform.TIKTOK].index(item.platform))
    notes = [_render_platform_status(status) for status in platform_statuses]
    return DiscoveryOutcome(candidates=final_candidates, platform_statuses=platform_statuses, notes=notes)
