from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from tracking.adapters.instagram import ApifyInstagramClient, seed_from_profiles
from tracking.adapters.tiktok import ApifyTikTokClient, extract_handle as extract_tiktok_handle, seed_from_feed_items
from tracking.models import Platform, TgUser, UserCompetitor
from tracking.services.competitor_service import upsert_competitor
from tracking.services.seed_resolver import SeedResolveError, resolve_seed_for_platform
from tracking.services.provider_config import get_instagram_apify_config, get_tiktok_apify_config
from tracking.services.provider_runtime import ProviderFetchCache


class LivePlatformReportError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedPlatformReport:
    user: TgUser
    resolved_rows: list[dict[str, str]]
    required_platforms: set[str]
    provider_fetch_cache: ProviderFetchCache


def _fetch_tiktok_seed_and_cache(*, raw_input: str, cache: ProviderFetchCache):
    config = get_tiktok_apify_config()
    profile_actor_id = str(getattr(config, "profile_actor_id", getattr(config, "actor_id", "")) or "")
    search_actor_id = str(getattr(config, "search_actor_id", "") or "")
    if config.provider != "apify":
        raise LivePlatformReportError(f"Unsupported TikTok provider: {config.provider}")
    if not config.access_token:
        raise LivePlatformReportError("TIKTOK_PROVIDER_ACCESS_TOKEN is not set")
    if not profile_actor_id:
        raise LivePlatformReportError("TIKTOK_APIFY_PROFILE_ACTOR_ID is not set")
    if not config.base_url:
        raise LivePlatformReportError("TIKTOK_PROVIDER_BASE_URL is not set")
    client = ApifyTikTokClient(
        access_token=config.access_token,
        actor_id=profile_actor_id,
        search_actor_id=search_actor_id,
        base_url=config.base_url,
    )
    try:
        max_results = max(
            1,
            min(
                int(getattr(settings, "YT_RECENT_N_FOR_METRICS", 15)),
                int(config.results_per_profile),
            ),
        )
        handle = str(extract_tiktok_handle(raw_input) or "").strip()
        if not handle:
            raise LivePlatformReportError(f"TikTok resolve failed for {raw_input}: invalid handle or profile URL")
        items = client.fetch_profile_feed(handle=handle, results_per_page=max_results)
    finally:
        client.close()
    seed = seed_from_feed_items(raw_input=raw_input, items=items)
    if seed and seed.handle:
        cache.store_tiktok_feed(handle=seed.handle, items=items)
    return seed


def _fetch_instagram_seed_and_cache(*, raw_input: str, cache: ProviderFetchCache):
    config = get_instagram_apify_config()
    profile_actor_id = str(getattr(config, "profile_actor_id", getattr(config, "actor_id", "")) or "")
    search_actor_id = str(getattr(config, "search_actor_id", "") or "")
    if config.provider != "apify":
        raise LivePlatformReportError(f"Unsupported Instagram provider: {config.provider}")
    if not config.access_token:
        raise LivePlatformReportError("INSTAGRAM_PROVIDER_ACCESS_TOKEN is not set")
    if not profile_actor_id:
        raise LivePlatformReportError("INSTAGRAM_APIFY_PROFILE_ACTOR_ID is not set")
    if not config.base_url:
        raise LivePlatformReportError("INSTAGRAM_PROVIDER_BASE_URL is not set")
    client = ApifyInstagramClient(
        access_token=config.access_token,
        actor_id=profile_actor_id,
        search_actor_id=search_actor_id,
        base_url=config.base_url,
    )
    try:
        profiles = client.fetch_profiles(inputs=[raw_input])
    finally:
        client.close()
    seed = seed_from_profiles(raw_input=raw_input, profiles=profiles)
    if profiles:
        cache.store_instagram_profile(profile=profiles[0], lookups=[raw_input])
    return seed


def prepare_live_platform_user(
    *,
    tg_user_id: int,
    tg_chat_id: int,
    timezone_str: str,
    entries: list[tuple[str, str]],
) -> PreparedPlatformReport:
    if not entries:
        raise LivePlatformReportError("Provide at least one platform input")

    user, _ = TgUser.objects.update_or_create(
        tg_user_id=int(tg_user_id),
        defaults={"tg_chat_id": int(tg_chat_id), "timezone_str": str(timezone_str or "UTC")},
    )
    UserCompetitor.objects.filter(user=user).update(is_active=False)

    resolved_rows: list[dict[str, str]] = []
    required_platforms: set[str] = set()
    provider_fetch_cache = ProviderFetchCache()
    for platform, raw in entries:
        try:
            if platform == Platform.TIKTOK:
                seed = _fetch_tiktok_seed_and_cache(raw_input=raw, cache=provider_fetch_cache)
            elif platform == Platform.INSTAGRAM:
                seed = _fetch_instagram_seed_and_cache(raw_input=raw, cache=provider_fetch_cache)
            else:
                seed = resolve_seed_for_platform(platform=platform, raw_input=raw)
        except LivePlatformReportError:
            raise
        except SeedResolveError as exc:
            raise LivePlatformReportError(f"{platform} resolve failed for {raw}: {exc}") from exc
        except Exception as exc:
            raise LivePlatformReportError(f"{platform} resolve failed for {raw}: {exc}") from exc
        if seed is None:
            raise LivePlatformReportError(f"{platform} resolve failed for {raw}: provider did not verify the profile")
        upsert_competitor(
            user=user,
            platform=seed.platform,
            external_id=seed.external_id,
            handle=seed.handle,
            url=seed.url,
            display_name=seed.title,
            added_by="manual",
            meta={"uploads_playlist_id": seed.uploads_playlist_id} if seed.uploads_playlist_id else None,
        )
        required_platforms.add(seed.platform)
        resolved_rows.append(
            {
                "platform": seed.platform,
                "input": raw,
                "external_id": seed.external_id,
                "handle": seed.handle or "",
                "url": seed.url,
            }
        )

    return PreparedPlatformReport(
        user=user,
        resolved_rows=resolved_rows,
        required_platforms=required_platforms,
        provider_fetch_cache=provider_fetch_cache,
    )
