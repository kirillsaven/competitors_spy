from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import re
from typing import Any

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
from tracking.services.youtube_service import get_recent_video_titles, get_youtube_client


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
    Platform.YOUTUBE: 3,
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
    Platform.INSTAGRAM: 3,
    Platform.TIKTOK: 5,
}
_DISCOVERY_EARLY_STOP_CANDIDATES = {
    Platform.YOUTUBE: 20,
    Platform.INSTAGRAM: 20,
    Platform.TIKTOK: 20,
}
_DISCOVERY_VALIDATION_BUDGET = {
    Platform.YOUTUBE: 25,
    Platform.INSTAGRAM: 25,
    Platform.TIKTOK: 25,
}
_DISCOVERY_VALIDATION_ITEMS = 5
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
    return f"recent::{platform}::{identity}::{max(1, int(n))}"


def _search_cache_key(platform: str, query: str) -> str:
    return f"search::{platform}::{' '.join(str(query or '').strip().lower().split())}"


def _retry_cache_key(*, layer: str, key: str) -> str:
    return f"setup-retry::{str(layer or '').strip()}::{str(key or '').strip()}"


def _provider_context_id(*, context: SetupRunContext | None = None, context_id: str | None = None) -> str:
    explicit = str(context_id or "").strip()
    if explicit:
        return explicit
    return str(getattr(context, "trace_id", "") or "").strip()


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
    store_retry_value(_retry_cache_key(layer="youtube-search", key=cache_key), list(channel_ids))
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
        cached = context.search_cache[cache_key]
        log_provider_call(
            actor=actor,
            platform=Platform.INSTAGRAM,
            purpose=purpose,
            cache="runtime_hit",
            normalized_input=" ".join(query.strip().lower().split()),
            requested_limit=limit,
            returned_count=len(cached),
            context_id=trace_id,
        )
        return [item for item in cached if isinstance(item, dict)]
    retry_cached, retry_found = get_cached_retry_value(_retry_cache_key(layer="instagram-search", key=cache_key))
    if retry_found and isinstance(retry_cached, list):
        if context is not None:
            context.search_cache[cache_key] = list(retry_cached)
        log_provider_call(
            actor=actor,
            platform=Platform.INSTAGRAM,
            purpose=purpose,
            cache="ttl_hit",
            normalized_input=" ".join(query.strip().lower().split()),
            requested_limit=limit,
            returned_count=len(retry_cached),
            context_id=trace_id,
        )
        return [item for item in retry_cached if isinstance(item, dict)]
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
        context.search_cache[cache_key] = list(filtered)
    store_retry_value(_retry_cache_key(layer="instagram-search", key=cache_key), list(filtered))
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
        cached = context.search_cache[cache_key]
        log_provider_call(
            actor=actor,
            platform=Platform.TIKTOK,
            purpose=purpose,
            cache="runtime_hit",
            normalized_input=" ".join(query.strip().lower().split()),
            requested_limit=limit,
            returned_count=len(cached),
            context_id=trace_id,
        )
        return [item for item in cached if isinstance(item, dict)]
    retry_cached, retry_found = get_cached_retry_value(_retry_cache_key(layer="tiktok-search", key=cache_key))
    if retry_found and isinstance(retry_cached, list):
        if context is not None:
            context.search_cache[cache_key] = list(retry_cached)
        log_provider_call(
            actor=actor,
            platform=Platform.TIKTOK,
            purpose=purpose,
            cache="ttl_hit",
            normalized_input=" ".join(query.strip().lower().split()),
            requested_limit=limit,
            returned_count=len(retry_cached),
            context_id=trace_id,
        )
        return [item for item in retry_cached if isinstance(item, dict)]
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
        context.search_cache[cache_key] = list(filtered)
    store_retry_value(_retry_cache_key(layer="tiktok-search", key=cache_key), list(filtered))
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
    if not anchor_stems:
        return False
    if len(full_stems) != len(specific_stems):
        return False
    return len(specific_stems & anchor_stems) <= 0


def _theme_anchor_stems(keywords: list[str]) -> set[str]:
    counts: dict[str, int] = {}
    for query in _dedupe_keyword_queries(keywords, max_queries=8):
        specific_stems = {stem for stem in _theme_token_stems(query) if stem not in _GENERIC_DISCOVERY_STEMS}
        if len(str(query or "").split()) >= 2 and len(specific_stems) >= 2:
            continue
        for stem in specific_stems:
            counts[stem] = counts.get(stem, 0) + 1
    anchors = {stem for stem, count in counts.items() if count >= 2}
    if anchors:
        return anchors
    strongest = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    return {stem for stem, _count in strongest[:2]}


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

    text_stem_sets = [_theme_token_stems(text) for text in texts if str(text or "").strip()]
    union_stems: set[str] = set()
    for stems in text_stem_sets:
        union_stems |= stems

    matched_phrases = 0
    anchor_overlap = len(union_stems & anchor_stems)
    specific_overlap = 0
    full_overlap = 0
    for full_stems, specific_stems in phrase_defs:
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


def _theme_profile_passes(*, candidate: _DiscoveryCandidate, keywords: list[str]) -> bool:
    texts = [
        str(candidate.display_name or "").strip(),
        str(candidate.description or "").strip(),
        str(candidate.handle or "").strip(),
    ]
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
    if metrics["anchor_overlap"] <= 0:
        return False
    return metrics["matched_phrases"] >= 1 or metrics["specific_overlap"] >= 2


def _theme_content_passes(*, candidate: _DiscoveryCandidate, texts: list[str], keywords: list[str]) -> bool:
    metrics = _theme_agreement_metrics(texts=texts, keywords=keywords)
    candidate.metadata["content_theme_score"] = (
        metrics["matched_phrases"] * 4
        + metrics["specific_overlap"]
        + metrics["strong_text_matches"] * 2
        + metrics["anchor_overlap"] * 2
        + metrics["strong_anchor_matches"] * 2
    )
    candidate.metadata["content_theme_matches"] = metrics["matched_phrases"]
    candidate.metadata["content_theme_anchor_overlap"] = metrics["anchor_overlap"]
    candidate.metadata["content_theme_specific_overlap"] = metrics["specific_overlap"]
    if metrics["anchor_overlap"] <= 0 or metrics["strong_anchor_matches"] <= 0:
        return False
    return (
        (metrics["matched_phrases"] >= 1 and metrics["strong_text_matches"] >= 1)
        or metrics["specific_overlap"] >= 3
        or metrics["strong_text_matches"] >= 2
    )


def _candidate_search_overlap(candidate: _DiscoveryCandidate) -> int:
    candidate_stems = _token_stems(candidate.handle, candidate.display_name, candidate.description)
    overlap = 0
    for query in candidate.query_hits:
        overlap += len(candidate_stems & _query_stems(query))
    return overlap


def _score_candidate(candidate: _DiscoveryCandidate) -> float:
    overlap = _candidate_search_overlap(candidate)
    query_repeat_bonus = len(candidate.query_hits) * 8.0
    overlap_bonus = overlap * 2.4
    description_bonus = 2.4 if len(str(candidate.description or "").strip()) >= 24 else 0.0
    cross_platform_bonus = len(candidate.cross_platform_keys) * 4.5
    verified_bonus = 1.4 if bool(candidate.metadata.get("verified")) else 0.0
    popularity_bonus = min(int(candidate.metadata.get("rank_hint") or 0), 1_000_000) / 250_000
    competitor_overlap_bonus = float(candidate.metadata.get("competitor_overlap") or 0) * 1.8
    collectible_count_bonus = min(int(candidate.metadata.get("collectible_count") or 0), 3) * 3.0
    collectible_views_bonus = min(int(candidate.metadata.get("collectible_views") or 0), 1_000_000) / 250_000
    profile_theme_bonus = float(candidate.metadata.get("profile_theme_score") or 0) * 2.0
    content_theme_bonus = float(candidate.metadata.get("content_theme_score") or 0) * 2.4
    return (
        query_repeat_bonus
        + overlap_bonus
        + description_bonus
        + cross_platform_bonus
        + verified_bonus
        + popularity_bonus
        + competitor_overlap_bonus
        + collectible_count_bonus
        + collectible_views_bonus
        + profile_theme_bonus
        + content_theme_bonus
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
    anchor_penalty = 14 if anchor_stems and anchor_overlap <= 0 else 0
    singleton_penalty = 6 if word_count == 1 and not specific_stems else 0
    return (
        anchor_overlap * 18
        + len(specific_stems) * 8
        + word_count * 2
        - len(generic_stems) * 3
        - anchor_penalty
        - singleton_penalty
    )


def _search_queries(keywords: list[str], *, max_queries: int = 6) -> list[str]:
    if max_queries <= 0:
        return []
    anchor_stems = _theme_anchor_stems(keywords)
    seen: set[str] = set()
    scored_queries: list[tuple[int, int, str, set[str]]] = []
    source_queries = list(keywords or [])
    source_queries.extend(_synthetic_search_queries(keywords, anchor_stems=anchor_stems))
    for index, raw in enumerate(source_queries):
        query = " ".join(str(raw or "").split()).strip()
        if len(query) < 3:
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
        score = _query_utility_score(query, anchor_stems=anchor_stems)
        scored_queries.append((
            score,
            index,
            query,
            coverage_stems,
        ))
    remaining = sorted(scored_queries, key=lambda item: (-item[0], item[1], item[2]))
    selected: list[str] = []
    covered_stems: set[str] = set()
    if remaining:
        score, index, query, stems = remaining.pop(0)
        selected.append(query)
        covered_stems |= stems
    while remaining and len(selected) < max_queries:
        best_idx = 0
        best_value: tuple[int, int, int, int, str] | None = None
        for idx, (score, index, query, stems) in enumerate(remaining):
            new_stems = len(stems - covered_stems)
            overlap = len(stems & covered_stems)
            value = (new_stems, score, -overlap, -index, query)
            if best_value is None or value > best_value:
                best_idx = idx
                best_value = value
        score, index, query, stems = remaining.pop(best_idx)
        selected.append(query)
        covered_stems |= stems
    return selected


def _synthetic_search_queries(keywords: list[str], *, anchor_stems: set[str]) -> list[str]:
    if not keywords:
        return []
    subject_terms: list[str] = []
    generic_stems: set[str] = set()
    for raw in keywords:
        query = " ".join(str(raw or "").split()).strip()
        if not query or len(query.split()) != 1:
            continue
        full_stems = _theme_token_stems(query)
        specific_stems = {stem for stem in full_stems if stem not in _GENERIC_DISCOVERY_STEMS}
        if specific_stems and (not anchor_stems or specific_stems & anchor_stems):
            subject_terms.append(query)
            continue
        generic_stems |= full_stems

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
    if re.fullmatch(r"[A-Za-z][A-Za-z\\s-]*", normalized_subject):
        base = normalized_subject
        variants: list[str] = []
        if generic_stems & _TEACHER_STEMS:
            variants.append(f"{base} teacher")
            variants.append(f"{base} tutor")
        if generic_stems & _LESSON_STEMS:
            variants.append(f"{base} lessons")
        if generic_stems & _GROUP_STEMS:
            variants.append(f"{base} teacher groups")
        return variants

    russian_object = _russian_subject_object_form(normalized_subject)
    variants = []
    if generic_stems & _TEACHER_STEMS:
        variants.append(f"репетитор {russian_object}")
        variants.append(f"преподаватель {russian_object}")
    if generic_stems & _LESSON_STEMS:
        variants.append(f"уроки {russian_object}")
    if generic_stems & _GROUP_STEMS:
        variants.append(f"группы {russian_object}")
    if generic_stems & _TEACHER_STEMS and generic_stems & _GROUP_STEMS:
        variants.append(f"группы преподавателей {russian_object}")
    return variants


def _russian_subject_object_form(term: str) -> str:
    value = str(term or "").strip().lower()
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


def _should_stop_discovery(*, platform: str, query_index: int, unique_candidates: int, max_candidates: int) -> bool:
    if platform in {Platform.INSTAGRAM, Platform.TIKTOK}:
        return False
    initial_budget = _DISCOVERY_INITIAL_QUERY_BUDGET.get(platform, 1)
    if query_index + 1 < initial_budget:
        return False
    enough_candidates = min(max_candidates, _DISCOVERY_EARLY_STOP_CANDIDATES.get(platform, 3))
    return unique_candidates >= enough_candidates


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


def _mark_candidate_collectible(candidate: _DiscoveryCandidate, *, item_count: int, max_views: int) -> None:
    candidate.metadata["collectible_count"] = max(1, int(item_count))
    candidate.metadata["collectible_views"] = max(0, int(max_views))


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
    cached = _get_cached_collectible_texts(
        platform=Platform.YOUTUBE,
        external_id=candidate.external_id,
        handle=candidate.handle,
        n=n,
        context=context,
    )
    if cached is not None:
        return cached

    client = get_youtube_client()
    try:
        items = client.channels_list(part="contentDetails", ids=[candidate.external_id])
        if not items:
            return _store_cached_collectible_texts(
                platform=Platform.YOUTUBE,
                external_id=candidate.external_id,
                handle=candidate.handle,
                n=n,
                texts=[],
                context=context,
            )
        uploads = ((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
        if not uploads:
            return _store_cached_collectible_texts(
                platform=Platform.YOUTUBE,
                external_id=candidate.external_id,
                handle=candidate.handle,
                n=n,
                texts=[],
                context=context,
            )
        playlist_items = client.playlist_items(playlist_id=str(uploads), max_results=max(6, n * 3))
        video_ids = playlist_items_to_video_ids(playlist_items)
        if not video_ids:
            return _store_cached_collectible_texts(
                platform=Platform.YOUTUBE,
                external_id=candidate.external_id,
                handle=candidate.handle,
                n=n,
                texts=[],
                context=context,
            )
        video_items = client.videos_list(ids=video_ids[: max(6, n * 3)], part="snippet,contentDetails")
        texts = [
            str(detail.title or "").strip()
            for detail in video_items_to_details(video_items)
            if detail.duration_seconds is not None and detail.duration_seconds <= 60 and str(detail.title or "").strip()
        ][:n]
        return _store_cached_collectible_texts(
            platform=Platform.YOUTUBE,
            external_id=candidate.external_id,
            handle=candidate.handle,
            n=n,
            texts=texts,
            context=context,
        )
    finally:
        client.close()


def _fetch_recent_instagram_reel_texts(
    *,
    candidate: _DiscoveryCandidate,
    n: int,
    context: SetupRunContext | None = None,
) -> tuple[list[str], list[int]]:
    cached = _get_cached_collectible_texts(
        platform=Platform.INSTAGRAM,
        external_id=candidate.external_id,
        handle=candidate.handle,
        n=n,
        context=context,
    )
    if cached is not None:
        return cached, []

    lookup = str(candidate.url or candidate.handle or candidate.external_id or "").strip()
    profiles = fetch_instagram_profiles_cached(
        inputs=[lookup],
        context=context,
        purpose="candidate_validation",
    )
    if not profiles:
        texts = _store_cached_collectible_texts(
            platform=Platform.INSTAGRAM,
            external_id=candidate.external_id,
            handle=candidate.handle,
            n=n,
            texts=[],
            context=context,
        )
        return texts, []
    details = profile_to_video_details(profiles[0])
    texts = [
        str(item.description or item.title or "").strip()
        for item in details[:n]
        if str(item.description or item.title or "").strip()
    ]
    texts = _store_cached_collectible_texts(
        platform=Platform.INSTAGRAM,
        external_id=candidate.external_id,
        handle=candidate.handle,
        n=n,
        texts=texts,
        context=context,
    )
    return texts, [int(item.views or 0) for item in details]


def _fetch_recent_tiktok_texts(
    *,
    candidate: _DiscoveryCandidate,
    n: int,
    context: SetupRunContext | None = None,
) -> tuple[list[str], list[int]]:
    cached = _get_cached_collectible_texts(
        platform=Platform.TIKTOK,
        external_id=candidate.external_id,
        handle=candidate.handle,
        n=n,
        context=context,
    )
    if cached is not None:
        return cached, []

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
    details = [item for item in details if int(item.views or 0) > 0]
    texts = [
        str(item.description or item.title or "").strip()
        for item in details[:n]
        if str(item.description or item.title or "").strip()
    ]
    texts = _store_cached_collectible_texts(
        platform=Platform.TIKTOK,
        external_id=candidate.external_id,
        handle=candidate.handle,
        n=n,
        texts=texts,
        context=context,
    )
    return texts, [int(item.views or 0) for item in details]


def _batch_fetch_recent_instagram_reel_texts(
    *,
    candidates: list[_DiscoveryCandidate],
    n: int,
    context: SetupRunContext | None = None,
) -> dict[str, tuple[list[str], list[int]]]:
    results: dict[str, tuple[list[str], list[int]]] = {}
    missing: list[_DiscoveryCandidate] = []
    for candidate in candidates:
        cached = _get_cached_collectible_texts(
            platform=Platform.INSTAGRAM,
            external_id=candidate.external_id,
            handle=candidate.handle,
            n=n,
            context=context,
        )
        if cached is not None:
            results[candidate.external_id] = (cached, [])
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
            texts = _store_cached_collectible_texts(
                platform=Platform.INSTAGRAM,
                external_id=candidate.external_id,
                handle=candidate.handle,
                n=n,
                texts=[],
                context=context,
            )
            results[candidate.external_id] = (texts, [])
            continue
        details = profile_to_video_details(profile)
        texts = [
            str(item.description or item.title or "").strip()
            for item in details[:n]
            if str(item.description or item.title or "").strip()
        ]
        texts = _store_cached_collectible_texts(
            platform=Platform.INSTAGRAM,
            external_id=candidate.external_id,
            handle=candidate.handle,
            n=n,
            texts=texts,
            context=context,
        )
        results[candidate.external_id] = (texts, [int(item.views or 0) for item in details])
    return results


def _batch_fetch_recent_tiktok_texts(
    *,
    candidates: list[_DiscoveryCandidate],
    n: int,
    context: SetupRunContext | None = None,
) -> dict[str, tuple[list[str], list[int]]]:
    results: dict[str, tuple[list[str], list[int]]] = {}
    missing: list[_DiscoveryCandidate] = []
    handles: list[str] = []
    for candidate in candidates:
        cached = _get_cached_collectible_texts(
            platform=Platform.TIKTOK,
            external_id=candidate.external_id,
            handle=candidate.handle,
            n=n,
            context=context,
        )
        if cached is not None:
            results[candidate.external_id] = (cached, [])
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
        details = [item for item in details if int(item.views or 0) > 0]
        texts = [
            str(item.description or item.title or "").strip()
            for item in details[:n]
            if str(item.description or item.title or "").strip()
        ]
        texts = _store_cached_collectible_texts(
            platform=Platform.TIKTOK,
            external_id=candidate.external_id,
            handle=candidate.handle,
            n=n,
            texts=texts,
            context=context,
        )
        results[candidate.external_id] = (texts, [int(item.views or 0) for item in details])
    return results


def _fetch_candidate_collectible_texts(
    *,
    candidate: _DiscoveryCandidate,
    context: SetupRunContext | None = None,
) -> tuple[list[str], list[int]]:
    if candidate.platform == Platform.YOUTUBE:
        return _fetch_recent_youtube_short_texts(candidate=candidate, n=_DISCOVERY_VALIDATION_ITEMS, context=context), []
    if candidate.platform == Platform.INSTAGRAM:
        return _fetch_recent_instagram_reel_texts(candidate=candidate, n=_DISCOVERY_VALIDATION_ITEMS, context=context)
    if candidate.platform == Platform.TIKTOK:
        return _fetch_recent_tiktok_texts(candidate=candidate, n=_DISCOVERY_VALIDATION_ITEMS, context=context)
    return [], []


def _validate_instagram_candidate_collectible(
    *,
    candidate: _DiscoveryCandidate,
    context: SetupRunContext | None = None,
) -> bool:
    texts, views = _fetch_recent_instagram_reel_texts(candidate=candidate, n=3, context=context)
    if not texts:
        return False
    _mark_candidate_collectible(
        candidate,
        item_count=len(texts),
        max_views=max(views) if views else 0,
    )
    return True


def _validate_tiktok_candidate_collectible(
    *,
    candidate: _DiscoveryCandidate,
    context: SetupRunContext | None = None,
) -> bool:
    texts, views = _fetch_recent_tiktok_texts(candidate=candidate, n=3, context=context)
    if not texts:
        return False
    _mark_candidate_collectible(
        candidate,
        item_count=len(texts),
        max_views=max(views) if views else 0,
    )
    return True


def _validate_youtube_candidate_collectible(
    *,
    candidate: _DiscoveryCandidate,
    context: SetupRunContext | None = None,
) -> bool:
    texts = _fetch_recent_youtube_short_texts(candidate=candidate, n=3, context=context)
    if not texts:
        return False
    _mark_candidate_collectible(
        candidate,
        item_count=len(texts),
        max_views=0,
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

    budget = min(len(ranked_candidates), max(1, _DISCOVERY_VALIDATION_BUDGET.get(platform, 1)))
    validated: list[_DiscoveryCandidate] = []
    candidate_batch = ranked_candidates[:budget]
    batch_texts: dict[str, tuple[list[str], list[int]]] = {}
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
            texts, views = batch_texts.get(candidate.external_id, ([], []))
        else:
            texts, views = _fetch_candidate_collectible_texts(candidate=candidate, context=context)
        if not texts:
            continue
        _mark_candidate_collectible(
            candidate,
            item_count=len(texts),
            max_views=max(views) if views else 0,
        )
        if not _theme_content_passes(candidate=candidate, texts=texts, keywords=keywords):
            continue
        validated.append(candidate)
        if len(validated) >= min(max_candidates, _DISCOVERY_EARLY_STOP_CANDIDATES.get(platform, max_candidates)):
            break
    if validated:
        ranked = sorted(
            validated,
            key=lambda item: (-_score_candidate(item), -len(item.query_hits), item.sort_tiebreak),
        )
        return ranked[:max_candidates], ""

    if platform == Platform.YOUTUBE:
        return [], "поиск выполнен, но топ-кандидаты не прошли проверку recent Shorts по теме."
    if platform == Platform.INSTAGRAM:
        return [], "поиск выполнен, но топ-кандидаты не прошли проверку reels по теме."
    return [], "поиск выполнен, но топ-кандидаты не прошли проверку recent TikTok-видео по теме."


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
        store_retry_value(_retry_cache_key(layer="recent-content", key=cache_key), list(texts))
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
    queries = _discovery_queries(
        platform=Platform.YOUTUBE,
        keywords=keywords,
        competitors=competitors,
        max_queries=max_search_calls,
    )
    if not queries:
        raise PlatformOnboardingError("No YouTube search queries could be built from niche keywords")
    query_hits: dict[str, set[str]] = {}
    for index, query in enumerate(queries):
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


def _search_instagram_candidates_raw(
    *,
    keywords: list[str],
    competitors: list[SeedResolution],
    max_candidates: int = 20,
    context: SetupRunContext | None = None,
) -> list[_DiscoveryCandidate]:
    queries = _discovery_queries(
        platform=Platform.INSTAGRAM,
        keywords=keywords,
        competitors=competitors,
        max_queries=_DISCOVERY_QUERY_BUDGET.get(Platform.INSTAGRAM, 2),
    )
    if not queries:
        raise PlatformOnboardingError("No Instagram search queries could be built from niche keywords")
    raw_by_id: dict[str, _DiscoveryCandidate] = {}
    result_limit = min(max_candidates, _DISCOVERY_RESULT_BUDGET[Platform.INSTAGRAM])
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

    ranked = sorted(
        raw_by_id.values(),
        key=lambda item: (-_score_candidate(item), -len(item.query_hits), item.sort_tiebreak),
    )
    return ranked[:max_candidates]


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
            max_candidates=max_candidates,
            context=context,
        )
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
    return ranked[:max_candidates]


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
