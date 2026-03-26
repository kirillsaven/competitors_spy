from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from tracking.models import Platform, TgUser, UserCompetitor
from tracking.services.competitor_service import upsert_competitor
from tracking.services.platform_onboarding import fetch_instagram_profiles_cached, fetch_tiktok_profile_feed_cached
from tracking.services.provider_runtime import ProviderFetchCache
from tracking.services.provider_config import get_tiktok_apify_config
from tracking.services.seed_resolver import SeedResolveError, resolve_seed_for_platform


class LivePlatformReportError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedPlatformReport:
    user: TgUser
    resolved_rows: list[dict[str, str]]
    required_platforms: set[str]
    provider_fetch_cache: ProviderFetchCache


def _fetch_tiktok_seed_and_cache(*, raw_input: str, cache: ProviderFetchCache):
    seed = resolve_seed_for_platform(platform=Platform.TIKTOK, raw_input=raw_input)
    if seed is None:
        return None
    config = get_tiktok_apify_config()
    max_results = max(
        1,
        min(
            int(getattr(settings, "YT_RECENT_N_FOR_METRICS", 15)),
            int(config.results_per_profile),
        ),
    )
    items = fetch_tiktok_profile_feed_cached(
        handle=str(seed.handle or "").strip(),
        results_per_page=max_results,
        purpose="live_report_seed",
        context_id=cache.context_id,
    )
    if seed.handle and items:
        cache.store_tiktok_feed(handle=seed.handle, items=items)
    return seed


def _fetch_instagram_seed_and_cache(*, raw_input: str, cache: ProviderFetchCache):
    seed = resolve_seed_for_platform(platform=Platform.INSTAGRAM, raw_input=raw_input)
    if seed is None:
        return None
    profiles = fetch_instagram_profiles_cached(
        inputs=[raw_input],
        purpose="live_report_seed",
        context_id=cache.context_id,
    )
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
