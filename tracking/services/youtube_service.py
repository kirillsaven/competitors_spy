from __future__ import annotations

import logging

from django.conf import settings

from common.text import extract_keywords

from tracking.adapters.base import CompetitorCandidate, SeedResolution
from tracking.adapters.youtube import (
    YouTubeApiError,
    YouTubeClient,
    channel_items_to_candidates,
    extract_featured_channel_ids,
    playlist_items_to_video_ids,
    resolve_seed_input,
)

logger = logging.getLogger(__name__)


class YouTubeNotConfigured(RuntimeError):
    pass


def get_youtube_client() -> YouTubeClient:
    if not getattr(settings, "YOUTUBE_API_KEY", ""):
        raise YouTubeNotConfigured("YOUTUBE_API_KEY is not set")
    return YouTubeClient(api_key=settings.YOUTUBE_API_KEY)


def resolve_youtube_seed(raw_input: str) -> SeedResolution | None:
    client = get_youtube_client()
    try:
        return resolve_seed_input(client, raw_input)
    finally:
        client.close()


def get_recent_video_titles(seed: SeedResolution, *, n: int = 10) -> list[str]:
    client = get_youtube_client()
    try:
        uploads = seed.uploads_playlist_id
        if not uploads:
            return []
        playlist_items = client.playlist_items(playlist_id=uploads, max_results=n)
        video_ids = playlist_items_to_video_ids(playlist_items)
        if not video_ids:
            return []
        vids = client.videos_list(ids=video_ids, part="snippet")
        titles: list[str] = []
        for it in vids:
            snippet = it.get("snippet") or {}
            title = snippet.get("title")
            if title:
                titles.append(str(title))
        return titles[:n]
    finally:
        client.close()


def infer_youtube_keywords(seed: SeedResolution) -> list[str]:
    """
    Keyword inference: channel title + description + last ~10 video titles.
    """
    client = get_youtube_client()
    try:
        parts: list[str] = []
        if seed.title:
            parts.append(seed.title)
        if seed.description:
            parts.append(seed.description)

        uploads = seed.uploads_playlist_id
        if uploads:
            playlist_items = client.playlist_items(playlist_id=uploads, max_results=10)
            video_ids = playlist_items_to_video_ids(playlist_items)
            if video_ids:
                vids = client.videos_list(ids=video_ids, part="snippet")
                for it in vids:
                    snippet = it.get("snippet") or {}
                    title = snippet.get("title")
                    if title:
                        parts.append(str(title))

        return extract_keywords("\n".join(parts), max_keywords=8)
    finally:
        client.close()


def discover_youtube_competitors(
    *,
    keywords: list[str],
    seed: SeedResolution | None,
    max_search_calls: int,
    max_candidates: int = 100,
    extra_featured_channel_ids: list[str] | None = None,
) -> list[CompetitorCandidate]:
    client = get_youtube_client()
    try:
        reason_by_id: dict[str, set[str]] = {}
        candidate_ids: list[str] = []

        def add_id(cid: str, reason: str) -> None:
            if not cid:
                return
            if seed and cid == seed.external_id:
                return
            if cid not in reason_by_id:
                reason_by_id[cid] = set()
                candidate_ids.append(cid)
            reason_by_id[cid].add(reason)

        # Featured channels from seed and (optionally) user-provided competitors.
        featured_sources: list[tuple[str, str]] = []
        if seed:
            featured_sources.append((seed.external_id, "featured_seed"))
        for cid in (extra_featured_channel_ids or [])[:3]:
            if cid:
                featured_sources.append((cid, "featured_comp"))

        for src_id, reason in featured_sources:
            try:
                sections = client.channel_sections(channel_id=src_id)
                for cid in extract_featured_channel_ids(sections):
                    add_id(cid, reason)
            except YouTubeApiError as e:
                logger.warning("Failed to load channel sections (%s): %s", src_id, e)

        # Keyword-based discovery (quota-limited).
        search_calls = max(0, int(max_search_calls))
        if search_calls > 0:
            seen_queries: set[str] = set()
            queries: list[str] = []

            def add_query(raw: str) -> None:
                normalized = " ".join(str(raw or "").split()).strip()
                if len(normalized) < 2:
                    return
                key = normalized.lower()
                if key in seen_queries:
                    return
                seen_queries.add(key)
                queries.append(normalized)

            add_query(" ".join((keywords or [])[:8]))
            for kw in (keywords or [])[:8]:
                add_query(str(kw or ""))

            calls_left = search_calls
            for q in queries:
                if calls_left <= 0:
                    break
                calls_left -= 1
                try:
                    for cid in client.search_channels(q=q, max_results=10):
                        add_id(cid, "search")
                except YouTubeApiError as e:
                    logger.warning("Failed to search channels (q=%r): %s", q, e)

            # Seed title/handle is a fallback only when keyword queries found nothing.
            if not candidate_ids and calls_left > 0 and seed:
                seed_queries: list[str] = []
                if seed.title:
                    seed_queries.append(str(seed.title))
                if seed.handle:
                    seed_queries.append(str(seed.handle).lstrip("@"))
                for q in seed_queries:
                    if calls_left <= 0:
                        break
                    calls_left -= 1
                    try:
                        for cid in client.search_channels(q=q, max_results=10):
                            add_id(cid, "search_seed")
                    except YouTubeApiError as e:
                        logger.warning("Failed to search seed channels (q=%r): %s", q, e)

        # Fetch channel metadata in batch.
        ranked_out: list[tuple[int, CompetitorCandidate]] = []
        for i in range(0, len(candidate_ids), 50):
            batch = candidate_ids[i : i + 50]
            items = client.channels_list(part="snippet,statistics", ids=batch)
            subs_by_id: dict[str, int] = {}
            for item in items:
                cid = str(item.get("id") or "")
                try:
                    subs_by_id[cid] = int(((item.get("statistics") or {}).get("subscriberCount")) or 0)
                except Exception:
                    subs_by_id[cid] = 0
            for cand in channel_items_to_candidates(items, reason=""):
                reasons = sorted(reason_by_id.get(cand.external_id, set()))
                ranked_out.append(
                    (
                        subs_by_id.get(cand.external_id, 0),
                    CompetitorCandidate(
                        platform=cand.platform,
                        external_id=cand.external_id,
                        handle=cand.handle,
                        url=cand.url,
                        display_name=cand.display_name,
                        reason=",".join(reasons) if reasons else "auto",
                    ),
                    )
                )

        # De-dup and cap.
        ranked_out.sort(key=lambda x: (-x[0], str(x[1].display_name or "").lower()))
        uniq: dict[str, CompetitorCandidate] = {}
        for _, c in ranked_out:
            if c.external_id not in uniq:
                uniq[c.external_id] = c
        return list(uniq.values())[:max_candidates]
    finally:
        client.close()


def search_youtube_seed_candidates(*, query: str, max_results: int = 8) -> list[SeedResolution]:
    """
    Best-effort search for a channel by a human-friendly nickname (e.g. Cyrillic).

    This uses search.list(type=channel) and is more quota-expensive than handle/channelId resolve.
    """
    q = (query or "").strip()
    if not q:
        return []
    client = get_youtube_client()
    try:
        ids = client.search_channels(q=q, max_results=max(1, min(int(max_results), 10)))
        # De-dup, preserve order.
        seen: set[str] = set()
        uniq: list[str] = []
        for cid in ids:
            if not cid or cid in seen:
                continue
            seen.add(cid)
            uniq.append(cid)

        if not uniq:
            return []

        items = client.channels_list(part="snippet,contentDetails,statistics", ids=uniq[:50])
        out: list[SeedResolution] = []
        for it in items:
            cid = it.get("id")
            snippet = it.get("snippet") or {}
            cd = it.get("contentDetails") or {}
            stats = it.get("statistics") or {}
            uploads = ((cd.get("relatedPlaylists") or {}).get("uploads")) if cd else None
            custom_url = snippet.get("customUrl") or ""
            handle = custom_url[1:] if isinstance(custom_url, str) and custom_url.startswith("@") else None
            try:
                subscriber_count = int(stats.get("subscriberCount")) if stats.get("subscriberCount") is not None else None
            except Exception:
                subscriber_count = None
            out.append(
                SeedResolution(
                    platform="youtube",
                    external_id=str(cid),
                    handle=handle,
                    url=f"https://www.youtube.com/channel/{cid}" if cid else "",
                    title=snippet.get("title"),
                    description=snippet.get("description"),
                    uploads_playlist_id=uploads,
                    subscriber_count=subscriber_count,
                )
            )
        out.sort(key=lambda x: (-(x.subscriber_count or 0), str(x.title or "").lower()))
        return out[: max_results]
    finally:
        client.close()
