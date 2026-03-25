from __future__ import annotations

from dataclasses import dataclass

from tracking.adapters.base import SeedResolution
from tracking.adapters.instagram import (
    ApifyInstagramClient,
    extract_handle as extract_instagram_handle,
    resolve_seed_input as resolve_instagram_seed_input,
)
from tracking.adapters.tiktok import (
    ApifyTikTokClient,
    extract_handle as extract_tiktok_handle,
    resolve_seed_input as resolve_tiktok_seed_input,
)
from tracking.adapters.youtube import (
    extract_channel_id as extract_youtube_channel_id,
    extract_handle as extract_youtube_handle,
    extract_video_id as extract_youtube_video_id,
)
from tracking.models import Platform
from tracking.services.provider_config import get_instagram_apify_config, get_tiktok_apify_config
from tracking.services.youtube_service import YouTubeNotConfigured, resolve_youtube_seed


class SeedResolveError(RuntimeError):
    pass


@dataclass(frozen=True)
class SeedResolveAttempt:
    platform: str
    seed: SeedResolution | None = None
    error: str | None = None


def _get_tiktok_client() -> ApifyTikTokClient:
    config = get_tiktok_apify_config()
    if config.provider != "apify":
        raise SeedResolveError(f"Unsupported TikTok provider: {config.provider}")
    if not config.access_token:
        raise SeedResolveError("TIKTOK_PROVIDER_ACCESS_TOKEN is not set")
    if not config.actor_id:
        raise SeedResolveError("TIKTOK_APIFY_PROFILE_ACTOR_ID is not set")
    if not config.base_url:
        raise SeedResolveError("TIKTOK_PROVIDER_BASE_URL is not set")
    return ApifyTikTokClient(
        access_token=config.access_token,
        actor_id=config.actor_id,
        base_url=config.base_url,
    )


def _get_instagram_client() -> ApifyInstagramClient:
    config = get_instagram_apify_config()
    if config.provider != "apify":
        raise SeedResolveError(f"Unsupported Instagram provider: {config.provider}")
    if not config.access_token:
        raise SeedResolveError("INSTAGRAM_PROVIDER_ACCESS_TOKEN is not set")
    if not config.actor_id:
        raise SeedResolveError("INSTAGRAM_APIFY_PROFILE_ACTOR_ID is not set")
    if not config.base_url:
        raise SeedResolveError("INSTAGRAM_PROVIDER_BASE_URL is not set")
    return ApifyInstagramClient(
        access_token=config.access_token,
        actor_id=config.actor_id,
        base_url=config.base_url,
    )


def _resolve_youtube_seed(raw_input: str) -> SeedResolution | None:
    try:
        return resolve_youtube_seed(raw_input)
    except YouTubeNotConfigured as exc:
        raise SeedResolveError(str(exc)) from exc


def _resolve_tiktok_seed(raw_input: str) -> SeedResolution | None:
    client = _get_tiktok_client()
    try:
        return resolve_tiktok_seed_input(client, raw_input)
    finally:
        client.close()


def _resolve_instagram_seed(raw_input: str) -> SeedResolution | None:
    client = _get_instagram_client()
    try:
        return resolve_instagram_seed_input(client, raw_input)
    finally:
        client.close()


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


def resolve_seed_for_platform(*, platform: str, raw_input: str) -> SeedResolution | None:
    platform_key = str(platform)
    if platform_key == Platform.YOUTUBE:
        return _resolve_youtube_seed(raw_input)
    if platform_key == Platform.TIKTOK:
        return _resolve_tiktok_seed(raw_input)
    if platform_key == Platform.INSTAGRAM:
        return _resolve_instagram_seed(raw_input)
    raise SeedResolveError(f"Unsupported platform: {platform}")


def attempt_exact_seed_resolution(raw_input: str) -> list[SeedResolveAttempt]:
    attempts: list[SeedResolveAttempt] = []
    for platform in candidate_platforms_for_exact_seed(raw_input):
        try:
            attempts.append(
                SeedResolveAttempt(
                    platform=platform,
                    seed=resolve_seed_for_platform(platform=platform, raw_input=raw_input),
                )
            )
        except SeedResolveError as exc:
            attempts.append(SeedResolveAttempt(platform=platform, error=str(exc)))
    return attempts


def resolve_exact_seed(raw_input: str) -> SeedResolution | None:
    attempts = attempt_exact_seed_resolution(raw_input)
    if not attempts:
        return None

    successes = [attempt.seed for attempt in attempts if attempt.seed is not None]
    errors = [attempt.error for attempt in attempts if attempt.error]
    if len(successes) == 1:
        if errors:
            raise SeedResolveError(
                f"Input could not be checked on every matching platform ({errors[0]}); send a full profile link to disambiguate."
            )
        return successes[0]
    if len(successes) > 1:
        platforms = ", ".join(sorted(str(seed.platform) for seed in successes))
        raise SeedResolveError(
            f"Input resolves on multiple platforms ({platforms}); send a full profile link to disambiguate."
        )

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
