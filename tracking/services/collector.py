from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings
from django.db import transaction

from tracking.adapters.registry import (
    get_refresh_competitor_handler,
    register_refresh_competitor_handler,
)
from tracking.adapters.instagram import (
    ApifyInstagramClient,
    GwaaInstagramClient,
    build_profile_url as build_instagram_profile_url,
    profile_to_video_details,
)
from tracking.adapters.tiktok import ApifyTikTokClient, build_profile_url, item_to_video_details
from tracking.adapters.youtube import (
    YouTubeApiError,
    YouTubeClient,
    playlist_items_to_video_ids,
    video_items_to_details,
)
from tracking.models import Competitor, ContentItem, MetricSnapshot, Platform
from tracking.services.provider_config import get_instagram_apify_config, get_tiktok_apify_config
from tracking.services.provider_runtime import ProviderFetchCache
from tracking.services.platform_onboarding import (
    PlatformOnboardingError,
    fetch_instagram_profiles_cached,
    fetch_tiktok_profile_feed_cached,
)
from tracking.services.youtube_service import YouTubeNotConfigured, get_youtube_client

logger = logging.getLogger(__name__)


class CollectorError(RuntimeError):
    pass


def _normalize_content_text_fields(*, title: str | None, description: str | None) -> tuple[str, str]:
    title_text = str(title or "")
    description_text = str(description or "")
    title_limit = int(ContentItem._meta.get_field("title").max_length or 500)
    if len(title_text) <= title_limit:
        return title_text, description_text
    if not description_text:
        description_text = title_text
    return title_text[:title_limit], description_text


def _get_youtube_client() -> YouTubeClient:
    try:
        return get_youtube_client()
    except YouTubeNotConfigured as exc:
        raise CollectorError(str(exc)) from exc


def _get_tiktok_client() -> ApifyTikTokClient:
    config = get_tiktok_apify_config()
    profile_actor_id = str(getattr(config, "profile_actor_id", getattr(config, "actor_id", "")) or "")
    search_actor_id = str(getattr(config, "search_actor_id", "") or "")
    if config.provider != "apify":
        raise CollectorError(f"Unsupported TikTok provider: {config.provider}")
    if not config.access_token:
        raise CollectorError("TIKTOK_PROVIDER_ACCESS_TOKEN is not set")
    if not profile_actor_id:
        raise CollectorError("TIKTOK_APIFY_PROFILE_ACTOR_ID is not set")
    if not config.base_url:
        raise CollectorError("TIKTOK_PROVIDER_BASE_URL is not set")
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
    if config.provider in {"gwaa", "public", "noauth"}:
        return GwaaInstagramClient(base_url=config.base_url)
    if config.provider != "apify":
        raise CollectorError(f"Unsupported Instagram provider: {config.provider}")
    if not config.access_token:
        raise CollectorError("INSTAGRAM_PROVIDER_ACCESS_TOKEN is not set")
    if not profile_actor_id:
        raise CollectorError("INSTAGRAM_APIFY_PROFILE_ACTOR_ID is not set")
    if not config.base_url:
        raise CollectorError("INSTAGRAM_PROVIDER_BASE_URL is not set")
    return ApifyInstagramClient(
        access_token=config.access_token,
        actor_id=profile_actor_id,
        search_actor_id=search_actor_id,
        base_url=config.base_url,
    )


def _youtube_short_reason(*, competitor: Competitor) -> str:
    return f"YouTube channel returned no recent Shorts with usable metrics: channel_id={competitor.external_id}"


def _instagram_reels_reason(*, username: str) -> str:
    return f"Instagram profile returned no recent reels with views: username={username}"


def _youtube_uploads_page_limit(*, mode: str) -> int:
    if mode == "full":
        return max(1, int(getattr(settings, "YT_UPLOADS_MAX_PAGES_SCAN_FULL", getattr(settings, "YT_UPLOADS_MAX_PAGES_SCAN", 4))))
    return max(1, int(getattr(settings, "YT_UPLOADS_MAX_PAGES_SCAN", 4)))


def _youtube_uploads_inspection_limit(*, mode: str) -> int:
    if mode == "full":
        return max(
            1,
            int(
                getattr(
                    settings,
                    "YT_UPLOADS_MAX_UPLOADS_INSPECTED_FULL",
                    getattr(settings, "YT_UPLOADS_MAX_UPLOADS_INSPECTED", 200),
                )
            ),
        )
    return max(1, int(getattr(settings, "YT_UPLOADS_MAX_UPLOADS_INSPECTED", 200)))


def _collect_youtube_short_details(
    *,
    client: YouTubeClient,
    uploads_playlist_id: str,
    target_short_count: int,
    max_pages_scanned: int,
    max_uploads_inspected: int,
) -> tuple[list[object], dict[str, int | bool]]:
    page_token: str | None = None
    pages_scanned = 0
    uploads_inspected = 0
    short_form_items_found = 0
    usable_details: list[object] = []
    seen_video_ids: set[str] = set()

    while (
        pages_scanned < max_pages_scanned
        and uploads_inspected < max_uploads_inspected
        and len(usable_details) < target_short_count
    ):
        remaining_upload_budget = max_uploads_inspected - uploads_inspected
        batch_size = min(50, remaining_upload_budget)
        if batch_size <= 0:
            break
        playlist_items, next_page_token = client.playlist_items_page(
            playlist_id=uploads_playlist_id,
            max_results=batch_size,
            page_token=page_token,
        )
        pages_scanned += 1
        if not playlist_items:
            break

        video_ids = playlist_items_to_video_ids(playlist_items)
        uploads_inspected += len(video_ids)
        if video_ids:
            video_items = client.videos_list(ids=video_ids, part="snippet,statistics,contentDetails")
            page_short_details = [
                item
                for item in video_items_to_details(video_items)
                if item.duration_seconds is not None and item.duration_seconds <= 60
            ]
            for item in page_short_details:
                if item.video_id in seen_video_ids:
                    continue
                seen_video_ids.add(item.video_id)
                short_form_items_found += 1
                if len(usable_details) < target_short_count:
                    usable_details.append(item)
        if not next_page_token:
            break
        page_token = next_page_token

    diagnostics: dict[str, int | bool] = {
        "uploads_pages_scanned": pages_scanned,
        "uploads_inspected": uploads_inspected,
        "short_form_items_found": short_form_items_found,
        "usable_short_form_items_returned": len(usable_details),
        "deeper_pages_used": pages_scanned > 1,
    }
    return usable_details, diagnostics


def refresh_youtube_competitor(
    *,
    competitor: Competitor,
    mode: str,
    captured_at: datetime,
    provider_fetch_cache: ProviderFetchCache | None = None,
) -> list[ContentItem]:
    if competitor.platform != Platform.YOUTUBE:
        return []
    max_results = int(getattr(settings, "YT_RECENT_N_FOR_METRICS", 15))
    if mode == "full":
        max_results = int(getattr(settings, "BASELINE_N", 30))
    target_short_count = max(1, int(max_results))
    max_pages_scanned = _youtube_uploads_page_limit(mode=mode)
    max_uploads_inspected = _youtube_uploads_inspection_limit(mode=mode)
    diagnostics: dict[str, int | bool] = {
        "uploads_pages_scanned": 0,
        "uploads_inspected": 0,
        "short_form_items_found": 0,
        "usable_short_form_items_returned": 0,
        "deeper_pages_used": False,
    }

    client = _get_youtube_client()
    try:
        meta = competitor.meta or {}
        uploads_playlist_id = meta.get("uploads_playlist_id")

        def refresh_uploads_playlist_id() -> str | None:
            items = client.channels_list(part="contentDetails,snippet", ids=[competitor.external_id])
            if not items:
                raise CollectorError(f"Channel not found: {competitor.external_id}")
            cd = items[0].get("contentDetails") or {}
            fresh_uploads_playlist_id = (cd.get("relatedPlaylists") or {}).get("uploads")
            if fresh_uploads_playlist_id:
                meta["uploads_playlist_id"] = fresh_uploads_playlist_id
                competitor.meta = meta
                competitor.display_name = (items[0].get("snippet") or {}).get("title") or competitor.display_name
                competitor.save(update_fields=["meta", "display_name"])
            return fresh_uploads_playlist_id

        if not uploads_playlist_id:
            uploads_playlist_id = refresh_uploads_playlist_id()

        if not uploads_playlist_id:
            raise CollectorError("uploads playlist id is missing")

        try:
            details, diagnostics = _collect_youtube_short_details(
                client=client,
                uploads_playlist_id=str(uploads_playlist_id),
                target_short_count=target_short_count,
                max_pages_scanned=max_pages_scanned,
                max_uploads_inspected=max_uploads_inspected,
            )
        except YouTubeApiError as e:
            # Playlist IDs can become stale/invalid for edge channels. Refresh channel data and retry once.
            if "playlistNotFound" not in str(e):
                raise
            logger.warning(
                "uploads playlist not found, refreshing channel metadata and retrying (competitor_id=%s, channel=%s)",
                competitor.id,
                competitor.external_id,
            )
            uploads_playlist_id = refresh_uploads_playlist_id()
            if not uploads_playlist_id:
                return []
            details, diagnostics = _collect_youtube_short_details(
                client=client,
                uploads_playlist_id=str(uploads_playlist_id),
                target_short_count=target_short_count,
                max_pages_scanned=max_pages_scanned,
                max_uploads_inspected=max_uploads_inspected,
            )
        if provider_fetch_cache is not None:
            provider_fetch_cache.store_youtube_refresh_diagnostics(
                competitor_id=competitor.id,
                diagnostics=diagnostics,
            )
        logger.info(
            "youtube_refresh_collection competitor_id=%s channel_id=%s mode=%s uploads_pages_scanned=%s "
            "uploads_inspected=%s short_form_items_found=%s usable_short_form_items_returned=%s "
            "target_short_count=%s max_pages_scanned=%s max_uploads_inspected=%s deeper_pages_used=%s",
            competitor.id,
            competitor.external_id,
            mode,
            diagnostics["uploads_pages_scanned"],
            diagnostics["uploads_inspected"],
            diagnostics["short_form_items_found"],
            diagnostics["usable_short_form_items_returned"],
            target_short_count,
            max_pages_scanned,
            max_uploads_inspected,
            diagnostics["deeper_pages_used"],
        )
        if not details:
            raise CollectorError(_youtube_short_reason(competitor=competitor))

        updated_items: list[ContentItem] = []
        with transaction.atomic():
            for v in details:
                title_text, description_text = _normalize_content_text_fields(title=v.title, description=v.description)
                obj, created = ContentItem.objects.get_or_create(
                    platform=Platform.YOUTUBE,
                    external_id=v.video_id,
                    defaults={
                        "competitor": competitor,
                        "url": v.url,
                        "title": title_text,
                        "description": description_text,
                        "published_at": v.published_at,
                        "duration_seconds": v.duration_seconds,
                        "meta": {"content_type": "short"},
                    },
                )
                # Keep mutable fields fresh.
                changed = False
                if obj.competitor_id != competitor.id:
                    obj.competitor = competitor
                    changed = True
                for field, value in [
                    ("url", v.url),
                    ("title", title_text),
                    ("description", description_text),
                    ("published_at", v.published_at),
                    ("duration_seconds", v.duration_seconds),
                ]:
                    if getattr(obj, field) != value and value is not None:
                        setattr(obj, field, value)
                        changed = True
                meta = dict(obj.meta or {})
                if meta.get("content_type") != "short":
                    meta["content_type"] = "short"
                    obj.meta = meta
                    changed = True
                if changed and not created:
                    obj.save()
                updated_items.append(obj)

                MetricSnapshot.objects.create(
                    content_item=obj,
                    captured_at=captured_at,
                    views=v.views,
                    likes=v.likes,
                    comments=v.comments,
                    shares=v.shares,
                    extra={},
                )

        return updated_items
    finally:
        client.close()


def refresh_tiktok_competitor(
    *,
    competitor: Competitor,
    mode: str,
    captured_at: datetime,
    provider_fetch_cache: ProviderFetchCache | None = None,
) -> list[ContentItem]:
    if competitor.platform != Platform.TIKTOK:
        return []
    if provider_fetch_cache is not None:
        cached_error = provider_fetch_cache.get_platform_error(platform=Platform.TIKTOK)
        if cached_error:
            raise CollectorError(cached_error)

    config = get_tiktok_apify_config()
    max_results = int(getattr(settings, "YT_RECENT_N_FOR_METRICS", 15))
    if mode == "full":
        max_results = int(getattr(settings, "BASELINE_N", 30))
    max_results = max(1, min(int(max_results), config.results_per_profile))

    handle = (competitor.handle or "").strip()
    if not handle:
        handle = str((competitor.meta or {}).get("profile_handle") or "").strip()
    if not handle and competitor.url:
        handle = str(competitor.url.rstrip("/").split("/")[-1]).lstrip("@")
    if not handle:
        handle = competitor.external_id
    handle = handle.lstrip("@")
    if not handle:
        raise CollectorError(f"TikTok competitor {competitor.id or competitor.external_id} has no resolvable handle")

    items = provider_fetch_cache.get_tiktok_feed(handle=handle) if provider_fetch_cache else None
    purpose = provider_fetch_cache.purpose if provider_fetch_cache else "report_collection"
    context_id = provider_fetch_cache.context_id if provider_fetch_cache else None
    try:
        if items is None:
            try:
                items = fetch_tiktok_profile_feed_cached(
                    handle=handle,
                    results_per_page=max_results,
                    purpose=purpose,
                    context_id=context_id,
                )
            except PlatformOnboardingError as exc:
                if provider_fetch_cache is not None:
                    provider_fetch_cache.mark_platform_error(platform=Platform.TIKTOK, reason=str(exc))
                raise CollectorError(str(exc)) from exc
            if provider_fetch_cache is not None and items:
                provider_fetch_cache.store_tiktok_feed(handle=handle, items=items)
        if not items:
            raise CollectorError(f"TikTok profile returned no items: handle={handle}")

        author_meta = items[0].get("authorMeta") or {}
        profile_handle = str(author_meta.get("name") or "").strip()
        if not profile_handle:
            raise CollectorError(f"TikTok provider response is missing author handle: handle={handle}")
        profile_url = build_profile_url(profile_handle)

        changed_fields: set[str] = set()
        competitor_meta = dict(competitor.meta or {})
        if competitor.handle != profile_handle:
            competitor.handle = profile_handle
            changed_fields.add("handle")
        if competitor.url != profile_url:
            competitor.url = profile_url
            changed_fields.add("url")
        display_name = str(author_meta.get("nickName") or "").strip()
        if display_name and competitor.display_name != display_name:
            competitor.display_name = display_name
            changed_fields.add("display_name")
        next_meta = {
            **competitor_meta,
            "profile_handle": profile_handle,
            "provider": "apify",
        }
        if author_meta.get("id"):
            next_meta["profile_id"] = str(author_meta.get("id"))
        signature = author_meta.get("signature")
        if signature:
            next_meta["signature"] = str(signature)
        if next_meta != competitor_meta:
            competitor.meta = next_meta
            changed_fields.add("meta")
        if changed_fields:
            competitor.save(update_fields=sorted(changed_fields))

        updated_items: list[ContentItem] = []
        with transaction.atomic():
            for item in items:
                details = item_to_video_details(item)
                title_text, description_text = _normalize_content_text_fields(
                    title=details.title,
                    description=details.description,
                )
                content_meta = {
                    "content_type": "slideshow" if bool(item.get("isSlideshow")) else "video",
                    "provider": "apify",
                }
                obj, created = ContentItem.objects.get_or_create(
                    platform=Platform.TIKTOK,
                    external_id=details.video_id,
                    defaults={
                        "competitor": competitor,
                        "url": details.url,
                        "title": title_text,
                        "description": description_text,
                        "published_at": details.published_at,
                        "duration_seconds": details.duration_seconds,
                        "meta": content_meta,
                    },
                )
                changed = False
                if obj.competitor_id != competitor.id:
                    obj.competitor = competitor
                    changed = True
                for field, value in [
                    ("url", details.url),
                    ("title", title_text),
                    ("description", description_text),
                    ("published_at", details.published_at),
                    ("duration_seconds", details.duration_seconds),
                ]:
                    if getattr(obj, field) != value and value is not None:
                        setattr(obj, field, value)
                        changed = True
                if obj.meta != content_meta:
                    obj.meta = content_meta
                    changed = True
                if changed and not created:
                    obj.save()
                updated_items.append(obj)

                MetricSnapshot.objects.create(
                    content_item=obj,
                    captured_at=captured_at,
                    views=details.views,
                    likes=details.likes,
                    comments=details.comments,
                    shares=details.shares,
                    extra={"provider": "apify"},
                )

        return updated_items
    finally:
        pass


def refresh_instagram_competitor(
    *,
    competitor: Competitor,
    mode: str,
    captured_at: datetime,
    provider_fetch_cache: ProviderFetchCache | None = None,
) -> list[ContentItem]:
    if competitor.platform != Platform.INSTAGRAM:
        return []
    if provider_fetch_cache is not None:
        cached_error = provider_fetch_cache.get_platform_error(platform=Platform.INSTAGRAM)
        if cached_error:
            raise CollectorError(cached_error)

    lookup = (competitor.handle or "").strip()
    if not lookup:
        lookup = competitor.url.strip()
    if not lookup:
        lookup = competitor.external_id.strip()
    if not lookup:
        raise CollectorError(f"Instagram competitor {competitor.id or competitor.external_id} has no resolvable lookup value")

    cached_profile = provider_fetch_cache.get_instagram_profile(lookup=lookup) if provider_fetch_cache else None
    purpose = provider_fetch_cache.purpose if provider_fetch_cache else "report_collection"
    context_id = provider_fetch_cache.context_id if provider_fetch_cache else None
    try:
        if cached_profile is not None:
            profiles = [cached_profile]
        else:
            try:
                profiles = fetch_instagram_profiles_cached(
                    inputs=[lookup],
                    purpose=purpose,
                    context_id=context_id,
                )
            except PlatformOnboardingError as exc:
                if provider_fetch_cache is not None:
                    provider_fetch_cache.mark_platform_error(platform=Platform.INSTAGRAM, reason=str(exc))
                raise CollectorError(str(exc)) from exc
        if not profiles:
            raise CollectorError(f"Instagram profile returned no items: lookup={lookup}")

        profile = profiles[0]
        if provider_fetch_cache is not None:
            provider_fetch_cache.store_instagram_profile(profile=profile, lookups=[lookup])
        username = str(profile.get("username") or "").strip()
        profile_id = str(profile.get("id") or "").strip()
        if not username:
            raise CollectorError(f"Instagram provider response is missing username: lookup={lookup}")
        if not profile_id:
            raise CollectorError(f"Instagram provider response is missing profile id: lookup={lookup}")

        details = profile_to_video_details(profile)
        if not details:
            raise CollectorError(_instagram_reels_reason(username=username))

        changed_fields: set[str] = set()
        competitor_meta = dict(competitor.meta or {})
        profile_url = str(profile.get("url") or build_instagram_profile_url(username))
        if competitor.handle != username:
            competitor.handle = username
            changed_fields.add("handle")
        if competitor.url != profile_url:
            competitor.url = profile_url
            changed_fields.add("url")
        display_name = str(profile.get("fullName") or "").strip()
        if display_name and competitor.display_name != display_name:
            competitor.display_name = display_name
            changed_fields.add("display_name")
        next_meta = {
            **competitor_meta,
            "profile_id": profile_id,
            "provider": "apify",
        }
        biography = profile.get("biography")
        if biography:
            next_meta["biography"] = str(biography)
        if next_meta != competitor_meta:
            competitor.meta = next_meta
            changed_fields.add("meta")
        if changed_fields:
            competitor.save(update_fields=sorted(changed_fields))

        updated_items: list[ContentItem] = []
        with transaction.atomic():
            for item in details:
                title_text, description_text = _normalize_content_text_fields(
                    title=item.title,
                    description=item.description,
                )
                content_meta = {"content_type": "reel", "provider": "apify"}
                obj, created = ContentItem.objects.get_or_create(
                    platform=Platform.INSTAGRAM,
                    external_id=item.video_id,
                    defaults={
                        "competitor": competitor,
                        "url": item.url,
                        "title": title_text,
                        "description": description_text,
                        "published_at": item.published_at,
                        "duration_seconds": item.duration_seconds,
                        "meta": content_meta,
                    },
                )
                changed = False
                if obj.competitor_id != competitor.id:
                    obj.competitor = competitor
                    changed = True
                for field, value in [
                    ("url", item.url),
                    ("title", title_text),
                    ("description", description_text),
                    ("published_at", item.published_at),
                    ("duration_seconds", item.duration_seconds),
                ]:
                    if getattr(obj, field) != value and value is not None:
                        setattr(obj, field, value)
                        changed = True
                if obj.meta != content_meta:
                    obj.meta = content_meta
                    changed = True
                if changed and not created:
                    obj.save()
                updated_items.append(obj)

                MetricSnapshot.objects.create(
                    content_item=obj,
                    captured_at=captured_at,
                    views=item.views,
                    likes=item.likes,
                    comments=item.comments,
                    shares=item.shares,
                    extra={"provider": "apify"},
                )

        return updated_items
    finally:
        pass


def refresh_competitor(
    *,
    competitor: Competitor,
    mode: str,
    captured_at: datetime,
    provider_fetch_cache: ProviderFetchCache | None = None,
) -> list[ContentItem]:
    handler = get_refresh_competitor_handler(competitor.platform)
    return handler(
        competitor=competitor,
        mode=mode,
        captured_at=captured_at,
        provider_fetch_cache=provider_fetch_cache,
    )


register_refresh_competitor_handler(Platform.YOUTUBE, refresh_youtube_competitor)
register_refresh_competitor_handler(Platform.TIKTOK, refresh_tiktok_competitor)
register_refresh_competitor_handler(Platform.INSTAGRAM, refresh_instagram_competitor)
