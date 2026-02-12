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
    max_candidates: int = 20,
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

        # Featured channels from seed channel sections.
        if seed:
            try:
                sections = client.channel_sections(channel_id=seed.external_id)
                for cid in extract_featured_channel_ids(sections):
                    add_id(cid, "featured")
            except YouTubeApiError as e:
                logger.warning("Failed to load channel sections: %s", e)

        # Keyword-based discovery (very limited due to quota).
        q = " ".join((keywords or [])[:8]).strip()
        if q and max_search_calls > 0:
            try:
                for cid in client.search_channels(q=q, max_results=10):
                    add_id(cid, "search")
            except YouTubeApiError as e:
                logger.warning("Failed to search channels: %s", e)

        # Fetch channel metadata in batch.
        out: list[CompetitorCandidate] = []
        for i in range(0, len(candidate_ids), 50):
            batch = candidate_ids[i : i + 50]
            items = client.channels_list(part="snippet", ids=batch)
            for cand in channel_items_to_candidates(items, reason=""):
                reasons = sorted(reason_by_id.get(cand.external_id, set()))
                out.append(
                    CompetitorCandidate(
                        platform=cand.platform,
                        external_id=cand.external_id,
                        handle=cand.handle,
                        url=cand.url,
                        display_name=cand.display_name,
                        reason=",".join(reasons) if reasons else "auto",
                    )
                )

        # De-dup and cap.
        uniq: dict[str, CompetitorCandidate] = {}
        for c in out:
            if c.external_id not in uniq:
                uniq[c.external_id] = c
        return list(uniq.values())[:max_candidates]
    finally:
        client.close()
