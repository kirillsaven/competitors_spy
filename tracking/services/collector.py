from __future__ import annotations

import logging
from datetime import datetime

from django.conf import settings
from django.db import transaction

from tracking.adapters.youtube import (
    YouTubeClient,
    playlist_items_to_video_ids,
    video_items_to_details,
)
from tracking.models import Competitor, ContentItem, MetricSnapshot, Platform

logger = logging.getLogger(__name__)


class CollectorError(RuntimeError):
    pass


def _get_youtube_client() -> YouTubeClient:
    api_key = getattr(settings, "YOUTUBE_API_KEY", "") or ""
    if not api_key:
        raise CollectorError("YOUTUBE_API_KEY is not set")
    return YouTubeClient(api_key=api_key)


def refresh_youtube_competitor(
    *,
    competitor: Competitor,
    mode: str,
    captured_at: datetime,
) -> list[ContentItem]:
    if competitor.platform != Platform.YOUTUBE:
        return []
    max_results = int(getattr(settings, "YT_RECENT_N_FOR_METRICS", 15))
    if mode == "full":
        max_results = int(getattr(settings, "BASELINE_N", 30))

    client = _get_youtube_client()
    try:
        meta = competitor.meta or {}
        uploads_playlist_id = meta.get("uploads_playlist_id")
        if not uploads_playlist_id:
            items = client.channels_list(part="contentDetails,snippet", ids=[competitor.external_id])
            if not items:
                raise CollectorError(f"Channel not found: {competitor.external_id}")
            cd = items[0].get("contentDetails") or {}
            uploads_playlist_id = (cd.get("relatedPlaylists") or {}).get("uploads")
            if uploads_playlist_id:
                meta["uploads_playlist_id"] = uploads_playlist_id
                competitor.meta = meta
                competitor.display_name = (items[0].get("snippet") or {}).get("title") or competitor.display_name
                competitor.save(update_fields=["meta", "display_name"])

        if not uploads_playlist_id:
            raise CollectorError("uploads playlist id is missing")

        playlist_items = client.playlist_items(playlist_id=str(uploads_playlist_id), max_results=max_results)
        video_ids = playlist_items_to_video_ids(playlist_items)
        if not video_ids:
            return []

        video_items = client.videos_list(ids=video_ids, part="snippet,statistics,contentDetails")
        details = video_items_to_details(video_items)

        updated_items: list[ContentItem] = []
        with transaction.atomic():
            for v in details:
                content_type = "short" if v.duration_seconds is not None and v.duration_seconds <= 60 else "video"
                obj, created = ContentItem.objects.get_or_create(
                    competitor=competitor,
                    platform=Platform.YOUTUBE,
                    external_id=v.video_id,
                    defaults={
                        "url": v.url,
                        "title": v.title,
                        "description": v.description,
                        "published_at": v.published_at,
                        "duration_seconds": v.duration_seconds,
                        "meta": {"content_type": content_type},
                    },
                )
                # Keep mutable fields fresh.
                changed = False
                for field, value in [
                    ("url", v.url),
                    ("title", v.title),
                    ("description", v.description),
                    ("published_at", v.published_at),
                    ("duration_seconds", v.duration_seconds),
                ]:
                    if getattr(obj, field) != value and value is not None:
                        setattr(obj, field, value)
                        changed = True
                meta = dict(obj.meta or {})
                if meta.get("content_type") != content_type:
                    meta["content_type"] = content_type
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
                    shares=None,
                    extra={},
                )

        return updated_items
    finally:
        client.close()
