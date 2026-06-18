from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import math
import re
from typing import Any, Iterable

from django.conf import settings
from django.utils import timezone

from common.text import KeywordSource, _TOKEN_RE, _normalize_token, _stem_token, extract_keywords
from common.time import parse_iso8601_duration_seconds

from tracking.adapters.youtube import YouTubeClient
from tracking.models import Competitor
from tracking.services.scoring import (
    _FORMAT_MARKER_STEMS,
    _INSTRUCTIONAL_MARKER_STEMS,
    _LOW_ADAPTATION_VALUE_STEMS,
    _dominant_script,
    _dominant_subject_cluster,
    _normalized_text,
    _text_stems,
    build_adaptation_context,
)
from tracking.services.youtube_service import get_youtube_client


logger = logging.getLogger(__name__)

_PROFILE_META_TEXT_KEYS = {"description", "biography", "signature", "about", "headline", "summary", "keywords"}
_SHORT_QUERY_STEMS = {_stem_token(value) for value in ("short", "shorts", "reel", "reels")}
_GENERIC_QUERY_STEMS = {
    _stem_token(value)
    for value in (
        "english",
        "language",
        "lesson",
        "lessons",
        "teacher",
        "tutor",
        "learn",
        "learning",
        "study",
        "course",
        "class",
        "video",
        "videos",
        "урок",
        "уроки",
        "язык",
        "английский",
        "репетитор",
        "преподаватель",
        "учить",
        "обучение",
        "занятия",
        "short",
        "shorts",
        "reel",
        "reels",
    )
}
_ADAPTABLE_MARKER_STEMS = {
    _stem_token(value)
    for value in (
        "example",
        "examples",
        "пример",
        "примеры",
        "dialogue",
        "dialog",
        "диалог",
        "practice",
        "практика",
        "exercise",
        "упражнение",
        "script",
        "checklist",
        "template",
        "plan",
        "разыграть",
        "phrase",
        "phrases",
        "фраза",
        "фразы",
    )
}
_SUPPLEMENTAL_LOW_ADAPTATION_STEMS = _LOW_ADAPTATION_VALUE_STEMS | {
    _stem_token(value)
    for value in (
        "song",
        "songs",
        "music",
        "lyrics",
        "lyric",
        "cover",
        "dance",
        "podcast",
        "interview",
        "news",
        "football",
        "movie",
        "film",
        "клип",
        "музыка",
        "песня",
        "песни",
        "текст",
        "новости",
        "фильм",
        "интервью",
    )
}
_SUPPLEMENTAL_SUBJECT_MARKERS = {
    "education_language": {
        _stem_token(value)
        for value in (
            "english",
            "language",
            "grammar",
            "vocabulary",
            "pronunciation",
            "speaking",
            "teacher",
            "tutor",
            "lesson",
            "exercise",
            "dialogue",
            "example",
            "английский",
            "язык",
            "грамматика",
            "словарь",
            "разговорный",
            "репетитор",
            "урок",
            "упражнение",
            "диалог",
            "фраза",
        )
    },
    "music_entertainment": {
        _stem_token(value)
        for value in (
            "music",
            "song",
            "songs",
            "lyrics",
            "lyric",
            "cover",
            "dance",
            "concert",
            "музыка",
            "песня",
            "песни",
            "текст",
            "танец",
            "кавер",
        )
    },
    "general_entertainment": {
        _stem_token(value)
        for value in (
            "prank",
            "meme",
            "gossip",
            "celebrity",
            "vlog",
            "challenge",
            "trend",
            "funny",
            "новости",
            "мем",
            "влог",
            "челлендж",
        )
    },
}
_SUPPLEMENTAL_MIN_SCORE = 0.35


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
    supplemental_score: float
    supplemental_ranking_factors: dict[str, float]
    supplemental_survival_reason: str
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
            "supplemental_score": self.supplemental_score,
            "supplemental_ranking_factors": dict(self.supplemental_ranking_factors),
            "supplemental_survival_reason": self.supplemental_survival_reason,
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


@dataclass(frozen=True)
class _SupplementalRankingDecision:
    score: float
    factors: dict[str, float]
    drop_reason: str | None
    survival_reason: str | None


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


def _sorted_factors(factors: dict[str, float]) -> dict[str, float]:
    return dict(sorted(factors.items(), key=lambda item: (-abs(item[1]), item[0])))


def _query_stems(query: str) -> set[str]:
    return {_stem_token(_normalize_token(token)) for token in _TOKEN_RE.findall(str(query or "")) if len(_normalize_token(token)) >= 3}


def _query_is_generic(query: str) -> bool:
    stems = _query_stems(query) - _SHORT_QUERY_STEMS
    if not stems:
        return True
    return not (stems - _GENERIC_QUERY_STEMS)


def _best_query_specificity(*, matched_queries: tuple[str, ...], normalized_text: str, keyword_stems: set[str]) -> tuple[int, int]:
    best_meaningful_stems = 0
    query_phrase_hits = 0
    for query in matched_queries:
        normalized_query = _normalize_query(query)
        if not normalized_query:
            continue
        stems = _query_stems(normalized_query) - _SHORT_QUERY_STEMS
        meaningful = stems - _GENERIC_QUERY_STEMS
        best_meaningful_stems = max(best_meaningful_stems, len(meaningful | (stems & keyword_stems)))
        if len(_TOKEN_RE.findall(normalized_query)) >= 2 and normalized_query in normalized_text:
            query_phrase_hits += 1
    return best_meaningful_stems, query_phrase_hits


def _supplemental_subject_cluster(stems: set[str]) -> str | None:
    best_cluster: str | None = None
    best_score = 0
    for cluster, markers in _SUPPLEMENTAL_SUBJECT_MARKERS.items():
        score = len(stems & markers)
        if score > best_score:
            best_cluster = cluster
            best_score = score
    return best_cluster if best_score >= 2 else None


def _supplemental_min_views_for_age_days(age_days: float) -> int:
    base = max(1, int(getattr(settings, "YT_SUPPLEMENTAL_MIN_VIEWS_BASE", 200) or 200))
    per_day = max(0, int(getattr(settings, "YT_SUPPLEMENTAL_MIN_VIEWS_PER_DAY", 120) or 120))
    cap = max(base, int(getattr(settings, "YT_SUPPLEMENTAL_MIN_VIEWS_CAP", 1200) or 1200))
    effective_age_days = max(float(age_days), 1.0)
    return min(cap, max(base, int(math.ceil(effective_age_days * per_day))))


def _compute_supplemental_candidate_ranking(
    *,
    candidate: YouTubeSupplementalVideoCandidate,
    now: datetime,
    max_age_days: int,
    keyword_stems: set[str],
    keyword_phrases: tuple[str, ...],
    linked_context_stems: set[str],
    competitor_context_stems: set[str],
    dominant_script: str | None,
    dominant_subject_cluster: str | None,
) -> _SupplementalRankingDecision:
    text = "\n".join(
        part
        for part in (
            candidate.title,
            candidate.description,
            candidate.channel_title or "",
        )
        if str(part or "").strip()
    )
    normalized_text = _normalized_text(text)
    item_stems = _text_stems(text)
    if not item_stems and not normalized_text:
        return _SupplementalRankingDecision(
            score=0.0,
            factors={},
            drop_reason="low_supplemental_score",
            survival_reason=None,
        )

    factors: dict[str, float] = {}
    keyword_overlap = len(item_stems & keyword_stems)
    phrase_matches = sum(1 for phrase in keyword_phrases if phrase in normalized_text)
    linked_overlap = len(item_stems & linked_context_stems)
    competitor_overlap = len(item_stems & competitor_context_stems)
    best_query_specificity, query_phrase_hits = _best_query_specificity(
        matched_queries=candidate.matched_queries,
        normalized_text=normalized_text,
        keyword_stems=keyword_stems,
    )
    generic_query_only = bool(candidate.matched_queries) and all(_query_is_generic(query) for query in candidate.matched_queries)
    topical_alignment = keyword_overlap + phrase_matches + query_phrase_hits

    if keyword_overlap:
        factors["niche_stem_overlap"] = min(keyword_overlap, 3) * 0.16
    if phrase_matches:
        factors["niche_phrase_match"] = min(phrase_matches, 2) * 0.28
    if query_phrase_hits:
        factors["query_phrase_match"] = min(query_phrase_hits, 2) * 0.18
    if best_query_specificity:
        factors["query_specificity_bonus"] = min(best_query_specificity, 3) * 0.08
    if linked_overlap and topical_alignment > 0:
        factors["linked_context_overlap"] = min(linked_overlap, 3) * 0.07
    if competitor_overlap and topical_alignment > 0:
        factors["competitor_context_overlap"] = min(competitor_overlap, 3) * 0.06

    instructional_hits = len(item_stems & _INSTRUCTIONAL_MARKER_STEMS)
    if instructional_hits and topical_alignment > 0:
        factors["instructional_markers"] = 0.14 + (min(instructional_hits, 3) * 0.04)
    format_hits = len(item_stems & (_FORMAT_MARKER_STEMS | _ADAPTABLE_MARKER_STEMS))
    if format_hits and topical_alignment > 0:
        factors["adaptable_format_markers"] = 0.10 + (min(format_hits, 3) * 0.03)

    age_days = max((now - candidate.published_at).total_seconds() / 86400.0, 0.0)
    freshness_ratio = max(0.0, 1.0 - (age_days / max(float(max_age_days), 1.0)))
    factors["freshness"] = round(0.12 * freshness_ratio, 4)

    traction_views = max(int(candidate.views or 0), 1)
    min_views_floor = _supplemental_min_views_for_age_days(age_days)
    factors["traction"] = round(min(math.log10(traction_views) / 20.0, 0.18), 4)
    if candidate.hit_count > 1:
        factors["multi_query_support"] = min((candidate.hit_count - 1) * 0.06, 0.18)
    if candidate.first_seen_rank is not None:
        factors["search_rank_bonus"] = round(max(0.0, 0.10 - (max(candidate.first_seen_rank, 1) - 1) * 0.01), 4)

    low_adaptation_hits = len(item_stems & _SUPPLEMENTAL_LOW_ADAPTATION_STEMS)
    item_subject_cluster = _supplemental_subject_cluster(item_stems) or _dominant_subject_cluster(item_stems)
    if dominant_subject_cluster and item_subject_cluster and item_subject_cluster != dominant_subject_cluster and topical_alignment <= 0:
        factors["subject_mismatch_penalty"] = -0.40

    item_script = _dominant_script(text)
    if dominant_script and item_script and item_script != dominant_script and topical_alignment <= 0:
        factors["language_mismatch_penalty"] = -0.25

    if low_adaptation_hits and topical_alignment <= 0:
        factors["off_topic_penalty"] = -0.24 - (min(low_adaptation_hits, 3) * 0.10)
    elif low_adaptation_hits and topical_alignment < 2:
        factors["low_adaptation_penalty"] = -0.12 - (min(low_adaptation_hits, 2) * 0.05)

    if generic_query_only:
        if (
            phrase_matches <= 0
            and query_phrase_hits <= 0
            and keyword_overlap <= 1
            and (linked_overlap + competitor_overlap) <= 1
            and low_adaptation_hits > 0
        ):
            factors["generic_query_only_penalty"] = -0.55
        elif topical_alignment <= 0 and best_query_specificity <= 1:
            factors["generic_query_only_penalty"] = -0.55
        elif phrase_matches <= 0 and query_phrase_hits <= 0 and (linked_overlap + competitor_overlap) <= 1:
            factors["generic_query_only_penalty"] = -0.24

    score = round(sum(factors.values()), 4)
    sorted_factors = _sorted_factors(factors)
    if float(sorted_factors.get("generic_query_only_penalty", 0.0)) <= -0.5:
        return _SupplementalRankingDecision(
            score=score,
            factors=sorted_factors,
            drop_reason="generic_query_weakness",
            survival_reason=None,
        )
    if low_adaptation_hits >= 2 and topical_alignment < 2 and instructional_hits <= 0 and format_hits <= 0:
        return _SupplementalRankingDecision(
            score=score,
            factors=sorted_factors,
            drop_reason="off_topic_penalty",
            survival_reason=None,
        )
    if any(name in sorted_factors for name in ("off_topic_penalty", "subject_mismatch_penalty", "language_mismatch_penalty")):
        negative_total = sum(
            float(sorted_factors.get(name, 0.0))
            for name in ("off_topic_penalty", "subject_mismatch_penalty", "language_mismatch_penalty")
        )
        positive_total = sum(max(float(value), 0.0) for value in sorted_factors.values())
        if negative_total <= -0.35 and positive_total < 0.45:
            return _SupplementalRankingDecision(
                score=score,
            factors=sorted_factors,
            drop_reason="off_topic_penalty",
            survival_reason=None,
        )
    if traction_views < min_views_floor:
        return _SupplementalRankingDecision(
            score=score,
            factors=sorted_factors,
            drop_reason="low_traction_quality",
            survival_reason=None,
        )
    if score < _SUPPLEMENTAL_MIN_SCORE:
        return _SupplementalRankingDecision(
            score=score,
            factors=sorted_factors,
            drop_reason="low_supplemental_score",
            survival_reason=None,
        )
    return _SupplementalRankingDecision(
        score=score,
        factors=sorted_factors,
        drop_reason=None,
        survival_reason="deterministic_rank_pass",
    )


def _candidate_title_signature(candidate: YouTubeSupplementalVideoCandidate) -> str:
    title = _normalized_text(candidate.title)
    if title:
        return title[:180]
    return _normalized_text(candidate.description)[:180]


def _candidate_meaningful_stems(candidate: YouTubeSupplementalVideoCandidate) -> set[str]:
    stems = _text_stems(candidate.title, candidate.description)
    return stems - _GENERIC_QUERY_STEMS - _SHORT_QUERY_STEMS


def _candidate_theme_key(candidate: YouTubeSupplementalVideoCandidate) -> str:
    best_query = ""
    best_specificity = -1
    for query in candidate.matched_queries:
        normalized_query = _normalize_query(query)
        if not normalized_query:
            continue
        specificity = len((_query_stems(normalized_query) - _GENERIC_QUERY_STEMS - _SHORT_QUERY_STEMS))
        if specificity > best_specificity:
            best_specificity = specificity
            best_query = normalized_query
    if best_query:
        return best_query
    stems = sorted(_candidate_meaningful_stems(candidate))
    return " ".join(stems[:3]).strip()


def _candidate_format_bucket(candidate: YouTubeSupplementalVideoCandidate) -> str:
    stems = _text_stems(candidate.title, candidate.description)
    if stems & _ADAPTABLE_MARKER_STEMS:
        return "adaptable_format"
    if stems & _INSTRUCTIONAL_MARKER_STEMS:
        return "instructional"
    if "?" in str(candidate.title or ""):
        return "question_hook"
    return "general"


def _is_near_duplicate_candidate(
    *,
    candidate: YouTubeSupplementalVideoCandidate,
    selected: list[YouTubeSupplementalVideoCandidate],
) -> bool:
    candidate_signature = _candidate_title_signature(candidate)
    candidate_stems = _candidate_meaningful_stems(candidate)
    for existing in selected:
        if candidate_signature and candidate_signature == _candidate_title_signature(existing):
            return True
        if candidate.channel_id and existing.channel_id and candidate.channel_id == existing.channel_id:
            overlap = candidate_stems & _candidate_meaningful_stems(existing)
            if candidate_stems and len(overlap) >= min(len(candidate_stems), 3):
                return True
        existing_stems = _candidate_meaningful_stems(existing)
        union = candidate_stems | existing_stems
        if union and len(candidate_stems & existing_stems) / len(union) >= 0.8:
            return True
    return False


def _shape_ranked_candidates(
    *,
    candidates: list[YouTubeSupplementalVideoCandidate],
    diagnostics: dict[str, Any],
) -> list[YouTubeSupplementalVideoCandidate]:
    max_final_candidates = max(1, int(getattr(settings, "YT_SUPPLEMENTAL_MAX_FINAL_CANDIDATES", 8) or 8))
    max_per_theme = max(1, int(getattr(settings, "YT_SUPPLEMENTAL_MAX_PER_THEME", 2) or 2))
    max_per_channel = max(1, int(getattr(settings, "YT_SUPPLEMENTAL_MAX_PER_CHANNEL", 2) or 2))
    max_consecutive_same_format = max(1, int(getattr(settings, "YT_SUPPLEMENTAL_MAX_CONSECUTIVE_FORMAT_BUCKET", 2) or 2))

    diagnostics["ranked_candidates_before_shaping"] = len(candidates)
    diagnostics["max_final_candidates"] = max_final_candidates
    diagnostics["max_per_theme"] = max_per_theme
    diagnostics["max_per_channel"] = max_per_channel
    diagnostics["max_consecutive_format_bucket"] = max_consecutive_same_format
    diagnostics["dropped_by_near_duplicate"] = 0
    diagnostics["dropped_by_same_theme_oversupply"] = 0
    diagnostics["dropped_by_per_channel_cap"] = 0
    diagnostics["dropped_by_format_bucket_run"] = 0

    selected: list[YouTubeSupplementalVideoCandidate] = []
    theme_counts: dict[str, int] = {}
    channel_counts: dict[str, int] = {}

    for candidate in candidates:
        if len(selected) >= max_final_candidates:
            break
        if _is_near_duplicate_candidate(candidate=candidate, selected=selected):
            diagnostics["dropped_by_near_duplicate"] += 1
            continue

        theme_key = _candidate_theme_key(candidate)
        if theme_key:
            if int(theme_counts.get(theme_key, 0) or 0) >= max_per_theme:
                diagnostics["dropped_by_same_theme_oversupply"] += 1
                continue

        channel_key = str(candidate.channel_id or candidate.channel_title or "").strip().lower()
        if channel_key:
            if int(channel_counts.get(channel_key, 0) or 0) >= max_per_channel:
                diagnostics["dropped_by_per_channel_cap"] += 1
                continue

        format_bucket = _candidate_format_bucket(candidate)
        if (
            theme_key
            and format_bucket
            and len(selected) >= max_consecutive_same_format
            and all(_candidate_format_bucket(existing) == format_bucket for existing in selected[-max_consecutive_same_format:])
            and all(_candidate_theme_key(existing) == theme_key for existing in selected[-max_consecutive_same_format:])
        ):
            diagnostics["dropped_by_format_bucket_run"] += 1
            continue

        selected.append(candidate)
        if theme_key:
            theme_counts[theme_key] = int(theme_counts.get(theme_key, 0) or 0) + 1
        if channel_key:
            channel_counts[channel_key] = int(channel_counts.get(channel_key, 0) or 0) + 1
    return selected


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
    adaptation_context = build_adaptation_context(
        niche_keywords=niche_keywords,
        linked_accounts=linked_accounts,
        competitors=competitors,
    )
    keyword_stems = set(adaptation_context.keyword_stems) if adaptation_context is not None else set()
    keyword_phrases = tuple(adaptation_context.keyword_phrases) if adaptation_context is not None else ()
    linked_context_stems = _text_stems(*_profile_texts(linked_accounts or []))
    competitor_context_stems = _text_stems(*_profile_texts(competitors or []))
    dominant_script = adaptation_context.dominant_script if adaptation_context is not None else None
    dominant_subject_cluster = adaptation_context.dominant_subject_cluster if adaptation_context is not None else None
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
        diagnostics["raw_candidates_before_ranking"] = 0
        diagnostics["dropped_by_generic_query_weakness"] = 0
        diagnostics["dropped_by_off_topic_penalty"] = 0
        diagnostics["dropped_by_low_traction_quality"] = 0
        diagnostics["dropped_by_low_supplemental_score"] = 0

        candidates: list[YouTubeSupplementalVideoCandidate] = []
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
            raw_candidate = YouTubeSupplementalVideoCandidate(
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
                supplemental_score=0.0,
                supplemental_ranking_factors={},
                supplemental_survival_reason="",
            )
            diagnostics["raw_candidates_before_ranking"] += 1
            ranking = _compute_supplemental_candidate_ranking(
                candidate=raw_candidate,
                now=current_time,
                max_age_days=max_age_days,
                keyword_stems=keyword_stems,
                keyword_phrases=keyword_phrases,
                linked_context_stems=linked_context_stems,
                competitor_context_stems=competitor_context_stems,
                dominant_script=dominant_script,
                dominant_subject_cluster=dominant_subject_cluster,
            )
            if ranking.drop_reason == "generic_query_weakness":
                diagnostics["dropped_by_generic_query_weakness"] += 1
                continue
            if ranking.drop_reason == "off_topic_penalty":
                diagnostics["dropped_by_off_topic_penalty"] += 1
                continue
            if ranking.drop_reason == "low_traction_quality":
                diagnostics["dropped_by_low_traction_quality"] += 1
                continue
            if ranking.drop_reason == "low_supplemental_score":
                diagnostics["dropped_by_low_supplemental_score"] += 1
                continue

            candidate = YouTubeSupplementalVideoCandidate(
                video_id=raw_candidate.video_id,
                url=raw_candidate.url,
                title=raw_candidate.title,
                description=raw_candidate.description,
                published_at=raw_candidate.published_at,
                duration_seconds=raw_candidate.duration_seconds,
                views=raw_candidate.views,
                likes=raw_candidate.likes,
                comments=raw_candidate.comments,
                channel_id=raw_candidate.channel_id,
                channel_title=raw_candidate.channel_title,
                matched_queries=raw_candidate.matched_queries,
                hit_count=raw_candidate.hit_count,
                first_seen_rank=raw_candidate.first_seen_rank,
                query_positions=raw_candidate.query_positions,
                supplemental_score=ranking.score,
                supplemental_ranking_factors=ranking.factors,
                supplemental_survival_reason=ranking.survival_reason or "deterministic_rank_pass",
            )
            candidates.append(candidate)

        candidates.sort(
            key=lambda item: (
                -item.supplemental_score,
                -item.hit_count,
                item.first_seen_rank if item.first_seen_rank is not None else 10**9,
            )
        )
        candidates = _shape_ranked_candidates(candidates=candidates, diagnostics=diagnostics)
        diagnostics["final_candidates"] = len(candidates)
        query_final_counts: dict[str, int] = {entry["query"]: 0 for entry in diagnostics["query_stats"]}
        for candidate in candidates:
            for matched_query in candidate.matched_queries:
                if matched_query in query_final_counts:
                    query_final_counts[matched_query] += 1
        for entry in diagnostics["query_stats"]:
            query = str(entry.get("query") or "")
            entry["final_candidates"] = int(query_final_counts.get(query, 0) or 0)

        logger.info(
            "youtube_topic_video_collection queries_built=%s queries_executed=%s search_hits_total=%s "
            "unique_video_hits=%s hydration_requested=%s hydrated_video_items=%s dropped_by_dedup=%s "
            "dropped_by_hydration_budget=%s filtered_missing_published_at=%s filtered_missing_metrics=%s "
            "filtered_too_old=%s filtered_non_short=%s raw_candidates_before_ranking=%s "
            "dropped_by_generic_query_weakness=%s dropped_by_off_topic_penalty=%s "
            "dropped_by_low_traction_quality=%s dropped_by_low_supplemental_score=%s ranked_candidates_before_shaping=%s "
            "dropped_by_near_duplicate=%s dropped_by_same_theme_oversupply=%s "
            "dropped_by_per_channel_cap=%s dropped_by_format_bucket_run=%s final_candidates=%s",
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
            diagnostics["raw_candidates_before_ranking"],
            diagnostics["dropped_by_generic_query_weakness"],
            diagnostics["dropped_by_off_topic_penalty"],
            diagnostics["dropped_by_low_traction_quality"],
            diagnostics["dropped_by_low_supplemental_score"],
            diagnostics["ranked_candidates_before_shaping"],
            diagnostics["dropped_by_near_duplicate"],
            diagnostics["dropped_by_same_theme_oversupply"],
            diagnostics["dropped_by_per_channel_cap"],
            diagnostics["dropped_by_format_bucket_run"],
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
