from __future__ import annotations

from urllib.parse import urlparse

from django.db.models import Count

from tracking.adapters.instagram import extract_handle as extract_instagram_handle
from tracking.adapters.tiktok import extract_handle as extract_tiktok_handle
from tracking.adapters.youtube import (
    extract_channel_id as extract_youtube_channel_id,
    extract_handle as extract_youtube_handle,
)
from tracking.models import Competitor, TgUser, UserCompetitor

PLATFORM_ORDER = ("youtube", "tiktok", "instagram")
PLATFORM_LABELS = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "instagram": "Instagram",
}


def upsert_competitor(
    *,
    user: TgUser,
    platform: str,
    external_id: str,
    handle: str | None,
    url: str,
    display_name: str | None,
    added_by: str,
    meta: dict | None = None,
) -> Competitor:
    obj, created = Competitor.objects.get_or_create(
        platform=platform,
        external_id=external_id,
        defaults={
            "handle": handle or "",
            "url": url or "",
            "display_name": display_name or "",
            "meta": meta or {},
        },
    )
    changed = False
    for field, value in [
        ("handle", handle or ""),
        ("url", url or ""),
        ("display_name", display_name or ""),
    ]:
        if getattr(obj, field) != value:
            setattr(obj, field, value)
            changed = True
    if meta:
        merged = dict(obj.meta or {})
        merged.update(meta)
        if merged != obj.meta:
            obj.meta = merged
            changed = True
    if changed and not created:
        obj.save()

    link, link_created = UserCompetitor.objects.get_or_create(
        user=user,
        competitor=obj,
        defaults={
            "added_by": added_by,
            "is_active": True,
        },
    )
    link_changed = False
    for field, value in [
        ("added_by", added_by),
        ("is_active", True),
    ]:
        if getattr(link, field) != value:
            setattr(link, field, value)
            link_changed = True
    if link_changed and not link_created:
        link.save()
    return obj


def list_active_user_competitor_links(*, user: TgUser) -> list[UserCompetitor]:
    return list(
        UserCompetitor.objects.select_related("competitor")
        .filter(user=user, is_active=True)
        .order_by("id")
    )


def list_active_user_competitor_links_grouped(*, user: TgUser) -> dict[str, list[UserCompetitor]]:
    grouped: dict[str, list[UserCompetitor]] = {platform: [] for platform in PLATFORM_ORDER}
    for link in list_active_user_competitor_links(user=user):
        grouped.setdefault(link.competitor.platform, []).append(link)
    return grouped


def get_active_user_competitor_counts(*, user: TgUser) -> dict[str, int]:
    counts = {platform: 0 for platform in PLATFORM_ORDER}
    rows = (
        UserCompetitor.objects.filter(user=user, is_active=True)
        .values("competitor__platform")
        .annotate(total=Count("id"))
    )
    for row in rows:
        platform = str(row.get("competitor__platform") or "")
        if platform:
            counts[platform] = int(row.get("total") or 0)
    return counts


def get_inactive_user_competitor_external_ids(*, user: TgUser, platform: str) -> set[str]:
    return {
        str(external_id).strip()
        for external_id in UserCompetitor.objects.filter(
            user=user,
            is_active=False,
            competitor__platform=platform,
        ).values_list("competitor__external_id", flat=True)
        if str(external_id).strip()
    }


def deactivate_user_competitors(*, user: TgUser, competitor_ids: list[int]) -> int:
    if not competitor_ids:
        return 0
    return int(
        UserCompetitor.objects.filter(
            user=user,
            competitor_id__in=competitor_ids,
            is_active=True,
        ).update(is_active=False)
    )


def find_active_competitor_link_match(*, active_links: list[UserCompetitor], raw_input: str) -> UserCompetitor | None:
    raw = str(raw_input or "").strip()
    if not raw or not active_links:
        return None

    normalized_url = _normalize_url(raw)
    if normalized_url:
        url_matches = [link for link in active_links if _normalize_url(link.competitor.url) == normalized_url]
        if len(url_matches) == 1:
            return url_matches[0]
        if len(url_matches) > 1:
            return None

    lookup_handles = {_normalize_handle(raw)}
    for extractor in (extract_youtube_handle, extract_tiktok_handle, extract_instagram_handle):
        handle = extractor(raw)
        if handle:
            lookup_handles.add(_normalize_handle(handle))
    lookup_handles.discard("")

    if lookup_handles:
        handle_matches = [link for link in active_links if _competitor_handle_keys(link.competitor) & lookup_handles]
        if len(handle_matches) == 1:
            return handle_matches[0]
        if len(handle_matches) > 1:
            return None

    channel_id = extract_youtube_channel_id(raw)
    external_id = str(channel_id or raw).strip()
    if external_id:
        external_matches = [link for link in active_links if str(link.competitor.external_id or "").strip() == external_id]
        if len(external_matches) == 1:
            return external_matches[0]

    return None


def _normalize_handle(value: str | None) -> str:
    return str(value or "").strip().lstrip("@").lower()


def _normalize_url(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw.rstrip("/").lower()
    if not parsed.netloc:
        return raw.rstrip("/").lower()
    path = (parsed.path or "").rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def _competitor_handle_keys(competitor: Competitor) -> set[str]:
    keys = {
        _normalize_handle(competitor.handle),
        _normalize_handle((competitor.meta or {}).get("profile_handle")),
    }
    for extractor in (extract_youtube_handle, extract_tiktok_handle, extract_instagram_handle):
        handle = extractor(competitor.url or "")
        if handle:
            keys.add(_normalize_handle(handle))
    keys.discard("")
    return keys
