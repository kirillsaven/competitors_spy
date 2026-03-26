from __future__ import annotations

from dataclasses import dataclass
from django.conf import settings

from tracking.adapters.base import SeedResolution
from tracking.adapters.instagram import extract_handle as extract_instagram_handle, seed_from_profiles
from tracking.adapters.tiktok import extract_handle as extract_tiktok_handle, seed_from_feed_items
from tracking.adapters.youtube import (
    extract_channel_id as extract_youtube_channel_id,
    extract_handle as extract_youtube_handle,
    extract_video_id as extract_youtube_video_id,
)
from tracking.models import Platform
from tracking.services.platform_onboarding import (
    fetch_instagram_profiles_cached,
    fetch_tiktok_profile_feed_cached,
)
from tracking.services.setup_retry_cache import get_cached_retry_value, store_retry_value
from tracking.services.setup_runtime import (
    SetupRunContext,
    get_platform_state,
    mark_platform_failure,
    platform_is_blocked,
)
from tracking.services.youtube_service import YouTubeNotConfigured, resolve_youtube_seed


class SeedResolveError(RuntimeError):
    pass


class SeedResolveAmbiguity(SeedResolveError):
    def __init__(self, *, candidates: list[SeedResolution], errors: list[str] | None = None) -> None:
        ordered = sorted(
            list(candidates or []),
            key=lambda seed: (str(seed.platform or ""), str(seed.title or seed.handle or "").lower(), seed.external_id),
        )
        self.candidates = ordered
        self.errors = [str(error or "").strip() for error in (errors or []) if str(error or "").strip()]
        platforms = ", ".join(sorted(str(seed.platform or "") for seed in ordered))
        message = f"Input resolves on multiple platforms ({platforms}); pick the intended profile."
        if self.errors:
            message += f" Other matching platforms could not be checked: {self.errors[0]}"
        super().__init__(message)


@dataclass(frozen=True)
class SeedResolveAttempt:
    platform: str
    seed: SeedResolution | None = None
    error: str | None = None


def _cache_key(*, platform: str, raw_input: str) -> str:
    return f"{str(platform).strip()}::{str(raw_input or '').strip().lower()}"


def _retry_cache_key(*, platform: str, raw_input: str) -> str:
    return f"setup-retry::seed::{_cache_key(platform=platform, raw_input=raw_input)}"


def _seed_retry_ttl_seconds(*, platform: str) -> int | None:
    if str(platform) == Platform.YOUTUBE:
        return int(getattr(settings, "YOUTUBE_SEED_SEARCH_CACHE_TTL_SECONDS", 21600) or 21600)
    return None


def _resolve_youtube_seed(raw_input: str) -> SeedResolution | None:
    try:
        return resolve_youtube_seed(raw_input)
    except YouTubeNotConfigured as exc:
        raise SeedResolveError(str(exc)) from exc


def _resolve_tiktok_seed(raw_input: str, *, context: SetupRunContext | None = None) -> SeedResolution | None:
    handle = extract_tiktok_handle(raw_input)
    if not handle:
        return None
    items = fetch_tiktok_profile_feed_cached(
        handle=handle,
        results_per_page=1,
        context=context,
        purpose="seed_resolution",
    )
    return seed_from_feed_items(raw_input=raw_input, items=items)


def _resolve_instagram_seed(raw_input: str, *, context: SetupRunContext | None = None) -> SeedResolution | None:
    lookup = (raw_input or "").strip()
    handle = extract_instagram_handle(raw_input)
    if not lookup or (not handle and not lookup.lower().startswith(("http://", "https://"))):
        return None
    profiles = fetch_instagram_profiles_cached(
        inputs=[lookup],
        context=context,
        purpose="seed_resolution",
    )
    return seed_from_profiles(raw_input=raw_input, profiles=profiles)


def candidate_platforms_for_exact_seed(raw_input: str) -> list[str]:
    raw = (raw_input or "").strip()
    if not raw:
        return []
    if extract_youtube_channel_id(raw) or extract_youtube_video_id(raw):
        return [Platform.YOUTUBE]
    platforms: list[str] = []
    if extract_youtube_handle(raw):
        platforms.append(Platform.YOUTUBE)
    if extract_tiktok_handle(raw):
        platforms.append(Platform.TIKTOK)
    if extract_instagram_handle(raw):
        platforms.append(Platform.INSTAGRAM)
    return platforms


def resolve_seed_for_platform(
    *,
    platform: str,
    raw_input: str,
    context: SetupRunContext | None = None,
) -> SeedResolution | None:
    platform_key = str(platform)
    if platform_is_blocked(context, platform_key):
        state = get_platform_state(context, platform_key)
        raise SeedResolveError(state.reason or f"{platform_key} is unavailable for this setup")
    cache_key = _cache_key(platform=platform_key, raw_input=raw_input)
    if context is not None and cache_key in context.seed_resolution_cache:
        cached_seed, cached_error = context.seed_resolution_cache[cache_key]
        if cached_error:
            raise SeedResolveError(cached_error)
        return cached_seed if isinstance(cached_seed, SeedResolution) else None
    retry_cached, retry_found = get_cached_retry_value(_retry_cache_key(platform=platform_key, raw_input=raw_input))
    if retry_found and isinstance(retry_cached, tuple) and len(retry_cached) == 2:
        cached_seed, cached_error = retry_cached
        if context is not None:
            context.seed_resolution_cache[cache_key] = (cached_seed, cached_error)
        if cached_error:
            raise SeedResolveError(str(cached_error))
        return cached_seed if isinstance(cached_seed, SeedResolution) else None
    try:
        if platform_key == Platform.YOUTUBE:
            seed = _resolve_youtube_seed(raw_input)
        elif platform_key == Platform.TIKTOK:
            seed = _resolve_tiktok_seed(raw_input, context=context)
        elif platform_key == Platform.INSTAGRAM:
            seed = _resolve_instagram_seed(raw_input, context=context)
        else:
            raise SeedResolveError(f"Unsupported platform: {platform}")
    except SeedResolveError as exc:
        if context is not None:
            context.seed_resolution_cache[cache_key] = (None, str(exc))
            mark_platform_failure(context, platform=platform_key, reason=str(exc))
        store_retry_value(
            _retry_cache_key(platform=platform_key, raw_input=raw_input),
            (None, str(exc)),
            ttl_seconds=_seed_retry_ttl_seconds(platform=platform_key),
        )
        raise
    except Exception as exc:
        if context is not None:
            context.seed_resolution_cache[cache_key] = (None, str(exc))
            mark_platform_failure(context, platform=platform_key, reason=str(exc))
        store_retry_value(
            _retry_cache_key(platform=platform_key, raw_input=raw_input),
            (None, str(exc)),
            ttl_seconds=_seed_retry_ttl_seconds(platform=platform_key),
        )
        raise SeedResolveError(str(exc)) from exc
    if context is not None:
        context.seed_resolution_cache[cache_key] = (seed, None)
    store_retry_value(
        _retry_cache_key(platform=platform_key, raw_input=raw_input),
        (seed, None),
        ttl_seconds=_seed_retry_ttl_seconds(platform=platform_key),
    )
    return seed


def attempt_exact_seed_resolution(
    raw_input: str,
    *,
    context: SetupRunContext | None = None,
) -> list[SeedResolveAttempt]:
    attempts: list[SeedResolveAttempt] = []
    for platform in candidate_platforms_for_exact_seed(raw_input):
        try:
            attempts.append(
                SeedResolveAttempt(
                    platform=platform,
                    seed=resolve_seed_for_platform(platform=platform, raw_input=raw_input, context=context),
                )
            )
        except SeedResolveError as exc:
            attempts.append(SeedResolveAttempt(platform=platform, error=str(exc)))
    return attempts


def resolve_exact_seed(raw_input: str, *, context: SetupRunContext | None = None) -> SeedResolution | None:
    if context is None:
        attempts = attempt_exact_seed_resolution(raw_input)
    else:
        attempts = attempt_exact_seed_resolution(raw_input, context=context)
    if not attempts:
        return None

    successes = [attempt.seed for attempt in attempts if attempt.seed is not None]
    errors = [attempt.error for attempt in attempts if attempt.error]
    if len(successes) == 1:
        return successes[0]
    if len(successes) > 1:
        raise SeedResolveAmbiguity(candidates=successes, errors=errors)

    if errors:
        raise SeedResolveError(errors[0] or "Seed resolution failed")
    return None


def can_search_youtube_seed_candidates(raw_input: str) -> bool:
    raw = (raw_input or "").strip()
    if not raw:
        return False
    if candidate_platforms_for_exact_seed(raw):
        return False
    if raw.lower().startswith(("http://", "https://")):
        return False
    return True
