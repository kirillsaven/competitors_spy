from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from tracking.adapters.instagram import seed_from_profiles
from tracking.adapters.tiktok import extract_handle as extract_tiktok_handle, seed_from_feed_items
from tracking.models import Platform, TgUser, UserCompetitor
from tracking.services.competitor_service import upsert_competitor
from tracking.services.platform_onboarding import fetch_instagram_profiles_cached, fetch_tiktok_profile_feed_cached
from tracking.services.seed_resolver import SeedResolveError, resolve_seed_for_platform
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
    max_results = max(1, int(getattr(settings, "YT_RECENT_N_FOR_METRICS", 15)))
    handle = str(extract_tiktok_handle(raw_input) or "").strip()
    if not handle:
        raise LivePlatformReportError(f"TikTok resolve failed for {raw_input}: invalid handle or profile URL")
    items = fetch_tiktok_profile_feed_cached(
        handle=handle,
        results_per_page=max_results,
        purpose="live_report_seed_resolve",
        context_id=cache.context_id,
    )
    seed = seed_from_feed_items(raw_input=raw_input, items=items)
    if seed and seed.handle:
        cache.store_tiktok_feed(handle=seed.handle, items=items)
    return seed


def _fetch_instagram_seed_and_cache(*, raw_input: str, cache: ProviderFetchCache):
    profiles = fetch_instagram_profiles_cached(
        inputs=[raw_input],
        purpose="live_report_seed_resolve",
        context_id=cache.context_id,
    )
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
    provider_fetch_cache = ProviderFetchCache(purpose="live_report")
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
