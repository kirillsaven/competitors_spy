from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import re
from typing import Any, Iterable

from django.conf import settings
from django.utils import timezone

from common.text import KeywordSource, _TOKEN_RE, _normalize_token, _stem_token, extract_keywords
from common.time import parse_iso8601_duration_seconds

from tracking.adapters.youtube import YouTubeClient
from tracking.models import Competitor
from tracking.services.youtube_service import get_youtube_client


logger = logging.getLogger(__name__)

_PROFILE_META_TEXT_KEYS = {"description", "biography", "signature", "about", "headline", "summary", "keywords"}
_SHORT_QUERY_STEMS = {_stem_token(value) for value in ("short", "shorts", "reel", "reels")}


@dataclass(frozen=True)
class YouTubeSupplementalVideoCandidate:
    video_id: str
    url: str
    title: str
    description: str
    published_at: datetime
    duration_seconds: int | None
    views: int
    likes: int | None
    comments: int | None
    channel_id: str | None
    channel_title: str | None
    matched_queries: tuple[str, ...]
    hit_count: int
    first_seen_rank: int | None
    query_positions: dict[str, int]
    source: str = "supplemental_topic_video"

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "video_id": self.video_id,
            "url": self.url,
            "title": self.title,
            "description": self.description,
            "published_at": self.published_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "duration_seconds": self.duration_seconds,
            "views": self.views,
            "likes": self.likes,
            "comments": self.comments,
            "channel_id": self.channel_id,
            "channel_title": self.channel_title,
            "matched_queries": list(self.matched_queries),
            "hit_count": self.hit_count,
            "first_seen_rank": self.first_seen_rank,
            "query_positions": dict(self.query_positions),
        }


@dataclass(frozen=True)
class YouTubeSupplementalCollectionResult:
    queries: tuple[str, ...]
    diagnostics: dict[str, Any]
    candidates: tuple[YouTubeSupplementalVideoCandidate, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": "supplemental_topic_video",
            "queries": list(self.queries),
            "diagnostics": dict(self.diagnostics),
            "candidates": [candidate.to_payload() for candidate in self.candidates],
        }


def _profile_texts(records: Iterable[Any]) -> list[str]:
    texts: list[str] = []
    for record in records:
        for attr in ("display_name", "handle", "title", "description"):
            value = str(getattr(record, attr, "") or "").strip()
            if value:
                texts.append(value)
        meta = getattr(record, "meta", None)
        if isinstance(meta, dict):
            for key, value in meta.items():
                if str(key or "").strip().lower() not in _PROFILE_META_TEXT_KEYS:
                    continue
                text = str(value or "").strip()
                if text:
                    texts.append(text)
    return texts


def _normalize_query(query: str) -> str:
    normalized = re.sub(r"\s+", " ", str(query or "").strip()).strip()
    if not normalized:
        return ""
    tokens = [_normalize_token(token) for token in _TOKEN_RE.findall(normalized) if len(_normalize_token(token)) >= 3]
    if not tokens:
        return ""
    compact = " ".join(tokens[:6]).strip()
    return compact[:100].strip()


def _query_sources(
    *,
    niche_keywords: list[str] | tuple[str, ...] | None,
    linked_accounts: Iterable[Any] | None,
    competitors: Iterable[Any] | None,
) -> list[KeywordSource]:
    sources: list[KeywordSource] = []
    for idx, keyword in enumerate(list(niche_keywords or [])):
        text = _normalize_query(str(keyword or ""))
        if text:
            sources.append(KeywordSource(text=text, source_id=f"niche-{idx}", source_type="description"))
    for idx, text in enumerate(_profile_texts(linked_accounts or [])):
        sources.append(KeywordSource(text=text, source_id=f"linked-{idx}", source_type="description"))
    for idx, text in enumerate(_profile_texts(competitors or [])):
        sources.append(KeywordSource(text=text, source_id=f"competitor-{idx}", source_type="competitor"))
    return sources


def build_youtube_topic_video_queries(
    *,
    niche_keywords: list[str] | tuple[str, ...] | None,
    linked_accounts: Iterable[Any] | None = None,
    competitors: Iterable[Any] | None = None,
) -> list[str]:
    max_queries = max(1, int(getattr(settings, "YT_SUPPLEMENTAL_MAX_QUERIES", 6) or 6))
    shorts_only = bool(getattr(settings, "YT_SUPPLEMENTAL_SHORTS_ONLY", True))
    seen: set[str] = set()
    queries: list[str] = []

    def add_query(raw: str) -> None:
        query = _normalize_query(raw)
        if not query:
            return
        key = query.lower()
        if key in seen:
            return
        seen.add(key)
        queries.append(query)

    for keyword in list(niche_keywords or []):
        add_query(str(keyword or ""))
        if len(queries) >= max_queries:
            break

    if len(queries) < max_queries:
        extracted = extract_keywords(
            _query_sources(
                niche_keywords=niche_keywords,
                linked_accounts=linked_accounts,
                competitors=competitors,
            ),
            max_keywords=max_queries * 2,
        )
        for candidate in extracted:
            add_query(candidate)
            if len(queries) >= max_queries:
                break

    if shorts_only and len(queries) < max_queries:
        base_queries = list(queries)
        for query in base_queries[: min(2, len(base_queries))]:
            stems = {_stem_token(token) for token in _TOKEN_RE.findall(query)}
            if stems & _SHORT_QUERY_STEMS:
                continue
            add_query(f"{query} shorts")
            if len(queries) >= max_queries:
                break

    return queries[:max_queries]


def _parse_published_at(raw_value: Any) -> datetime | None:
    raw = str(raw_value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        published_at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    return published_at


def _search_hit_video_id(item: dict[str, Any]) -> str:
    return str(((item.get("id") or {}).get("videoId")) or "").strip()


def collect_youtube_topic_video_candidates(
    *,
    niche_keywords: list[str] | tuple[str, ...] | None,
    linked_accounts: Iterable[Any] | None = None,
    competitors: Iterable[Competitor] | None = None,
    now: datetime | None = None,
    client: YouTubeClient | None = None,
) -> YouTubeSupplementalCollectionResult:
    current_time = now or timezone.now()
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)

    max_queries = max(1, int(getattr(settings, "YT_SUPPLEMENTAL_MAX_QUERIES", 6) or 6))
    max_results_per_query = max(1, min(int(getattr(settings, "YT_SUPPLEMENTAL_MAX_RESULTS_PER_QUERY", 5) or 5), 25))
    max_search_pages = max(1, int(getattr(settings, "YT_SUPPLEMENTAL_MAX_SEARCH_PAGES_PER_QUERY", 1) or 1))
    max_hydrated_videos = max(1, int(getattr(settings, "YT_SUPPLEMENTAL_MAX_HYDRATED_VIDEOS", 20) or 20))
    max_age_days = max(1, int(getattr(settings, "YT_SUPPLEMENTAL_MAX_AGE_DAYS", 30) or 30))
    shorts_only = bool(getattr(settings, "YT_SUPPLEMENTAL_SHORTS_ONLY", True))
    published_after = current_time - timedelta(days=max_age_days)

    queries = tuple(
        build_youtube_topic_video_queries(
            niche_keywords=niche_keywords,
            linked_accounts=linked_accounts,
            competitors=competitors,
        )[:max_queries]
    )
    diagnostics: dict[str, Any] = {
        "queries_built": len(queries),
        "queries_executed": 0,
        "max_queries": max_queries,
        "max_results_per_query": max_results_per_query,
        "max_search_pages_per_query": max_search_pages,
        "max_hydrated_videos": max_hydrated_videos,
        "max_age_days": max_age_days,
        "shorts_only": shorts_only,
        "search_hits_total": 0,
        "unique_video_hits": 0,
        "hydration_requested": 0,
        "hydrated_video_items": 0,
        "hydration_misses": 0,
        "dropped_by_dedup": 0,
        "dropped_by_hydration_budget": 0,
        "filtered_missing_published_at": 0,
        "filtered_missing_metrics": 0,
        "filtered_too_old": 0,
        "filtered_non_short": 0,
        "final_candidates": 0,
        "query_stats": [],
    }
    if not queries:
        return YouTubeSupplementalCollectionResult(queries=(), diagnostics=diagnostics, candidates=())

    own_client = client is None
    youtube_client = client or get_youtube_client()
    provenance_by_video_id: dict[str, dict[str, Any]] = {}
    hydration_ids: list[str] = []

    try:
        for query in queries:
            page_token: str | None = None
            query_pages_scanned = 0
            query_search_hits = 0
            query_unique_hits = 0

            while query_pages_scanned < max_search_pages:
                items, next_page_token = youtube_client.search_videos_page(
                    q=query,
                    max_results=max_results_per_query,
                    page_token=page_token,
                    published_after=published_after,
                    short_duration_only=shorts_only,
                )
                query_pages_scanned += 1
                diagnostics["queries_executed"] += 1 if query_pages_scanned == 1 else 0
                for rank_offset, item in enumerate(items, start=1):
                    video_id = _search_hit_video_id(item)
                    if not video_id:
                        continue
                    query_search_hits += 1
                    diagnostics["search_hits_total"] += 1
                    absolute_rank = ((query_pages_scanned - 1) * max_results_per_query) + rank_offset
                    provenance = provenance_by_video_id.get(video_id)
                    if provenance is None:
                        provenance = {
                            "matched_queries": [],
                            "query_positions": {},
                            "hit_count": 0,
                            "first_seen_rank": absolute_rank,
                            "channel_id": str((item.get("snippet") or {}).get("channelId") or "").strip() or None,
                            "channel_title": str((item.get("snippet") or {}).get("channelTitle") or "").strip() or None,
                        }
                        provenance_by_video_id[video_id] = provenance
                        diagnostics["unique_video_hits"] += 1
                        query_unique_hits += 1
                        if len(hydration_ids) < max_hydrated_videos:
                            hydration_ids.append(video_id)
                        else:
                            diagnostics["dropped_by_hydration_budget"] += 1
                    else:
                        diagnostics["dropped_by_dedup"] += 1
                    provenance["hit_count"] = int(provenance.get("hit_count") or 0) + 1
                    matched_queries = list(provenance.get("matched_queries") or [])
                    if query not in matched_queries:
                        matched_queries.append(query)
                    provenance["matched_queries"] = matched_queries
                    query_positions = dict(provenance.get("query_positions") or {})
                    query_positions.setdefault(query, absolute_rank)
                    provenance["query_positions"] = query_positions

                if not next_page_token:
                    break
                page_token = next_page_token

            diagnostics["query_stats"].append(
                {
                    "query": query,
                    "pages_scanned": query_pages_scanned,
                    "search_hits": query_search_hits,
                    "unique_hits": query_unique_hits,
                    "final_candidates": 0,
                }
            )

        diagnostics["hydration_requested"] = len(hydration_ids)
        hydrated_items = (
            youtube_client.videos_list(ids=hydration_ids, part="snippet,statistics,contentDetails")
            if hydration_ids
            else []
        )
        diagnostics["hydrated_video_items"] = len(hydrated_items)
        diagnostics["hydration_misses"] = max(len(hydration_ids) - len(hydrated_items), 0)

        candidates: list[YouTubeSupplementalVideoCandidate] = []
        query_final_counts: dict[str, int] = {entry["query"]: 0 for entry in diagnostics["query_stats"]}
        for item in hydrated_items:
            video_id = str(item.get("id") or "").strip()
            if not video_id:
                continue
            snippet = item.get("snippet") or {}
            statistics = item.get("statistics") or {}
            content_details = item.get("contentDetails") or {}
            published_at = _parse_published_at(snippet.get("publishedAt"))
            if published_at is None:
                diagnostics["filtered_missing_published_at"] += 1
                continue
            if published_at < published_after:
                diagnostics["filtered_too_old"] += 1
                continue
            views_raw = statistics.get("viewCount")
            if views_raw is None:
                diagnostics["filtered_missing_metrics"] += 1
                continue
            try:
                views = int(views_raw)
            except (TypeError, ValueError):
                diagnostics["filtered_missing_metrics"] += 1
                continue
            duration_seconds = parse_iso8601_duration_seconds(content_details.get("duration") or "")
            if shorts_only and (duration_seconds is None or duration_seconds > 60):
                diagnostics["filtered_non_short"] += 1
                continue

            provenance = provenance_by_video_id.get(video_id) or {}
            candidate = YouTubeSupplementalVideoCandidate(
                video_id=video_id,
                url=f"https://www.youtube.com/watch?v={video_id}",
                title=str(snippet.get("title") or "").strip(),
                description=str(snippet.get("description") or "").strip(),
                published_at=published_at,
                duration_seconds=duration_seconds,
                views=views,
                likes=int(statistics["likeCount"]) if statistics.get("likeCount") is not None else None,
                comments=int(statistics["commentCount"]) if statistics.get("commentCount") is not None else None,
                channel_id=provenance.get("channel_id"),
                channel_title=provenance.get("channel_title"),
                matched_queries=tuple(provenance.get("matched_queries") or ()),
                hit_count=int(provenance.get("hit_count") or 0),
                first_seen_rank=provenance.get("first_seen_rank"),
                query_positions=dict(provenance.get("query_positions") or {}),
            )
            candidates.append(candidate)
            for matched_query in candidate.matched_queries:
                if matched_query in query_final_counts:
                    query_final_counts[matched_query] += 1

        candidates.sort(key=lambda item: (item.first_seen_rank if item.first_seen_rank is not None else 10**9, -item.hit_count))
        diagnostics["final_candidates"] = len(candidates)
        for entry in diagnostics["query_stats"]:
            query = str(entry.get("query") or "")
            entry["final_candidates"] = int(query_final_counts.get(query, 0) or 0)

        logger.info(
            "youtube_topic_video_collection queries_built=%s queries_executed=%s search_hits_total=%s "
            "unique_video_hits=%s hydration_requested=%s hydrated_video_items=%s dropped_by_dedup=%s "
            "dropped_by_hydration_budget=%s filtered_missing_published_at=%s filtered_missing_metrics=%s "
            "filtered_too_old=%s filtered_non_short=%s final_candidates=%s",
            diagnostics["queries_built"],
            diagnostics["queries_executed"],
            diagnostics["search_hits_total"],
            diagnostics["unique_video_hits"],
            diagnostics["hydration_requested"],
            diagnostics["hydrated_video_items"],
            diagnostics["dropped_by_dedup"],
            diagnostics["dropped_by_hydration_budget"],
            diagnostics["filtered_missing_published_at"],
            diagnostics["filtered_missing_metrics"],
            diagnostics["filtered_too_old"],
            diagnostics["filtered_non_short"],
            diagnostics["final_candidates"],
        )
        return YouTubeSupplementalCollectionResult(
            queries=queries,
            diagnostics=diagnostics,
            candidates=tuple(candidates),
        )
    finally:
        if own_client:
            youtube_client.close()
