from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
import re
from typing import Any, Iterable

from django.conf import settings

from common.text import _normalize_token, _stem_token
from common.stats import iqr, median

from tracking.models import Competitor, CompetitorBaseline, ContentItem, MetricSnapshot, Platform


EPS = 1e-6
_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_META_TEXT_KEYS = {"description", "biography", "signature", "about", "headline", "summary", "keywords"}
_INSTRUCTIONAL_MARKER_STEMS = {
    _stem_token(value)
    for value in (
        "lesson",
        "урок",
        "guide",
        "гайд",
        "tutorial",
        "how",
        "разбор",
        "tips",
        "tip",
        "совет",
        "mistakes",
        "ошибки",
        "grammar",
        "vocabulary",
        "pronunciation",
        "example",
        "examples",
        "exercise",
        "упражнение",
    )
}
_FORMAT_MARKER_STEMS = {
    _stem_token(value)
    for value in (
        "checklist",
        "чеклист",
        "template",
        "script",
        "formula",
        "framework",
        "dialogue",
        "dialog",
        "диалог",
        "разбор",
        "example",
        "examples",
        "step",
        "steps",
        "plan",
    )
}
_LOW_ADAPTATION_VALUE_STEMS = {
    _stem_token(value)
    for value in (
        "viral",
        "funny",
        "meme",
        "prank",
        "drama",
        "gossip",
        "celebrity",
        "dating",
        "reaction",
        "react",
        "challenge",
        "trend",
        "trending",
        "asmr",
        "unboxing",
        "shopping",
        "giveaway",
        "luxury",
        "flex",
        "vlog",
        "shorts",
    )
}
_SUBJECT_CLUSTER_MARKERS = {
    "education_language": {
        _stem_token(value)
        for value in (
            "english",
            "language",
            "grammar",
            "vocabulary",
            "pronunciation",
            "speaking",
            "lesson",
            "teacher",
            "tutor",
            "английский",
            "язык",
            "грамматика",
            "словарь",
            "разговорный",
            "репетитор",
            "урок",
        )
    },
    "beauty": {
        _stem_token(value)
        for value in ("makeup", "skincare", "beauty", "cosmetic", "nails", "hair", "макияж", "косметика", "маникюр")
    },
    "food": {_stem_token(value) for value in ("recipe", "cook", "cooking", "meal", "baking", "food", "рецепт", "еда", "кухня")},
    "fitness": {
        _stem_token(value)
        for value in ("workout", "fitness", "gym", "diet", "yoga", "weightloss", "тренировка", "фитнес", "диета")
    },
    "gaming": {
        _stem_token(value)
        for value in ("game", "gaming", "minecraft", "fortnite", "dota", "stream", "стрим", "игра")
    },
    "finance": {
        _stem_token(value)
        for value in ("crypto", "trading", "invest", "investment", "bitcoin", "forex", "stock", "крипта", "инвестиции")
    },
    "entertainment": {
        _stem_token(value)
        for value in ("prank", "meme", "celebrity", "gossip", "drama", "funny", "dance", "dating", "влог", "мем")
    },
}


def _is_youtube_short_item(item: ContentItem) -> bool:
    if item.platform != Platform.YOUTUBE:
        return False
    meta = item.meta if isinstance(item.meta, dict) else {}
    return str(meta.get("content_type") or "").strip().lower() == "short"


@dataclass(frozen=True)
class BaselineMetrics:
    vph_median: float
    vph_iqr: float
    er_median: float | None
    er_iqr: float | None
    n: int
    rph_median: float | None = None
    rph_iqr: float | None = None


@dataclass(frozen=True)
class AdaptationContext:
    niche_keywords: tuple[str, ...]
    keyword_stems: frozenset[str]
    keyword_phrases: tuple[str, ...]
    account_stems: frozenset[str]
    dominant_script: str | None
    dominant_subject_cluster: str | None


@dataclass(frozen=True)
class ScoredItem:
    content_item: ContentItem
    competitor: Competitor
    views_end: int
    likes_end: int | None
    comments_end: int | None
    shares_end: int | None
    velocity: float  # views/hour, either delta-based or current average since publish
    score_type: str  # "delta" | "current_vph"
    delta_views: int | None
    delta_hours: float | None
    er_end: float | None
    base_score: float
    adaptation_relevance_score: float
    adaptation_relevance_factors: dict[str, float]
    selection_path: str
    fallback_reason: str | None
    score: float


def _normalized_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower()).strip()


def _text_tokens(*values: str | None) -> list[str]:
    tokens: list[str] = []
    for value in values:
        for raw in _TOKEN_RE.findall(str(value or "")):
            token = _normalize_token(raw)
            if len(token) >= 3:
                tokens.append(token)
    return tokens


def _text_stems(*values: str | None) -> set[str]:
    return {_stem_token(token) for token in _text_tokens(*values)}


def _dominant_script(*values: str | None) -> str | None:
    latin = 0
    cyrillic = 0
    for token in _text_tokens(*values):
        if re.search(r"[a-z]", token, re.IGNORECASE):
            latin += 1
        if re.search(r"[а-яё]", token, re.IGNORECASE):
            cyrillic += 1
    total = latin + cyrillic
    if total < 4:
        return None
    if latin / total >= 0.7:
        return "latin"
    if cyrillic / total >= 0.7:
        return "cyrillic"
    return None


def _dominant_subject_cluster(stems: set[str]) -> str | None:
    best_cluster: str | None = None
    best_score = 0
    for cluster, markers in _SUBJECT_CLUSTER_MARKERS.items():
        score = len(stems & markers)
        if score > best_score:
            best_cluster = cluster
            best_score = score
    return best_cluster if best_score >= 2 else None


def _iter_profile_texts(records: Iterable[Any]) -> list[str]:
    texts: list[str] = []
    for record in records:
        for attr in ("display_name", "handle", "title", "description"):
            value = str(getattr(record, attr, "") or "").strip()
            if value:
                texts.append(value)
        meta = getattr(record, "meta", None)
        if isinstance(meta, dict):
            for key, value in meta.items():
                if str(key or "").strip().lower() not in _META_TEXT_KEYS:
                    continue
                text = str(value or "").strip()
                if text:
                    texts.append(text)
    return texts


def build_adaptation_context(
    *,
    niche_keywords: list[str] | tuple[str, ...] | None,
    linked_accounts: Iterable[Any] | None = None,
    competitors: Iterable[Any] | None = None,
) -> AdaptationContext | None:
    normalized_keywords = tuple(
        phrase
        for phrase in (_normalized_text(keyword) for keyword in list(niche_keywords or []))
        if phrase
    )
    keyword_stems = frozenset(_text_stems(*normalized_keywords))
    keyword_phrases = tuple(
        phrase for phrase in normalized_keywords if 2 <= len(_text_tokens(phrase)) <= 5
    )
    account_texts = _iter_profile_texts(linked_accounts or [])
    competitor_texts = _iter_profile_texts(competitors or [])
    account_stems = frozenset(_text_stems(*(account_texts + competitor_texts)) - set(keyword_stems))
    context_texts = list(normalized_keywords) + account_texts + competitor_texts
    if not keyword_stems and not account_stems and not keyword_phrases:
        return None
    return AdaptationContext(
        niche_keywords=normalized_keywords,
        keyword_stems=keyword_stems,
        keyword_phrases=keyword_phrases,
        account_stems=account_stems,
        dominant_script=_dominant_script(*context_texts),
        dominant_subject_cluster=_dominant_subject_cluster(set(keyword_stems) | set(account_stems)),
    )


def _competitor_profile_stems(competitor: Competitor) -> set[str]:
    return _text_stems(*_iter_profile_texts([competitor]))


def _compute_adaptation_relevance(
    *,
    item: ContentItem,
    competitor: Competitor,
    adaptation_context: AdaptationContext | None,
) -> tuple[float, dict[str, float]]:
    if adaptation_context is None:
        return 0.0, {}

    text = f"{item.title or ''}\n{item.description or ''}"
    normalized_text = _normalized_text(text)
    item_stems = _text_stems(text)
    if not item_stems and not normalized_text:
        return 0.0, {}

    factors: dict[str, float] = {}
    keyword_overlap = len(item_stems & set(adaptation_context.keyword_stems))
    phrase_matches = sum(1 for phrase in adaptation_context.keyword_phrases if phrase in normalized_text)
    account_overlap = len(item_stems & set(adaptation_context.account_stems))
    competitor_overlap = len(item_stems & _competitor_profile_stems(competitor))
    topical_alignment = keyword_overlap + phrase_matches

    if keyword_overlap:
        factors["niche_stem_overlap"] = min(keyword_overlap, 4) * 0.22
    if phrase_matches:
        factors["niche_phrase_match"] = min(phrase_matches, 2) * 0.30
    if topical_alignment > 0 and account_overlap:
        factors["account_context_overlap"] = min(account_overlap, 3) * 0.08
    if topical_alignment > 0 and competitor_overlap:
        factors["competitor_profile_overlap"] = min(competitor_overlap, 3) * 0.06

    instructional_hits = len(item_stems & _INSTRUCTIONAL_MARKER_STEMS)
    if topical_alignment > 0 and instructional_hits:
        factors["instructional_markers"] = 0.18 + (min(instructional_hits, 3) * 0.04)
    format_hits = len(item_stems & _FORMAT_MARKER_STEMS)
    if topical_alignment > 0 and format_hits:
        factors["format_markers"] = 0.10 + (min(format_hits, 2) * 0.03)
    if topical_alignment >= 2 and competitor_overlap > 0:
        factors["repeated_theme_boost"] = 0.12

    item_cluster = _dominant_subject_cluster(item_stems)
    if (
        adaptation_context.dominant_subject_cluster
        and item_cluster
        and item_cluster != adaptation_context.dominant_subject_cluster
        and topical_alignment <= 0
    ):
        factors["subject_mismatch_penalty"] = -0.45

    item_script = _dominant_script(text)
    if (
        adaptation_context.dominant_script
        and item_script
        and item_script != adaptation_context.dominant_script
        and topical_alignment <= 0
    ):
        factors["language_mismatch_penalty"] = -0.25

    low_adaptation_hits = len(item_stems & _LOW_ADAPTATION_VALUE_STEMS)
    if topical_alignment <= 0 and low_adaptation_hits:
        factors["off_topic_penalty"] = -0.18 - (min(low_adaptation_hits, 3) * 0.09)

    adaptation_score = round(sum(factors.values()), 4)
    return adaptation_score, dict(sorted(factors.items(), key=lambda item: (-abs(item[1]), item[0])))


def _fallback_candidate_passes(
    *,
    adaptation_relevance_score: float,
    adaptation_relevance_factors: dict[str, float],
    delta_views: int,
    effective_min_delta: int,
) -> tuple[bool, str | None]:
    min_adaptation_score = float(getattr(settings, "REPORT_FALLBACK_MIN_ADAPTATION_SCORE", 0.45) or 0.45)
    delta_ratio = float(getattr(settings, "REPORT_FALLBACK_DELTA_RATIO", 0.5) or 0.5)
    relaxed_min_delta = max(int(effective_min_delta * delta_ratio), 20)
    has_negative_penalty = any(float(value) < 0 for value in (adaptation_relevance_factors or {}).values())
    if adaptation_relevance_score < min_adaptation_score:
        return False, None
    if has_negative_penalty:
        return False, None
    if delta_views < relaxed_min_delta:
        return False, None
    return True, "delta_threshold_relaxed"


def compute_competitor_baseline(*, competitor: Competitor, now: datetime) -> BaselineMetrics:
    window_days = int(getattr(settings, "BASELINE_WINDOW_DAYS", 30))
    n_items = int(getattr(settings, "BASELINE_N", 30))
    window_start = now - timedelta(days=window_days)

    items = (
        ContentItem.objects.filter(competitor=competitor, published_at__gte=window_start)
        .order_by("-published_at")
        .all()[:n_items]
    )
    if competitor.platform == Platform.YOUTUBE:
        items = [item for item in items if _is_youtube_short_item(item)]

    vph_values: list[float] = []
    er_values: list[float] = []
    rph_values: list[float] = []

    for item in items:
        snap = MetricSnapshot.objects.filter(content_item=item).order_by("-captured_at").first()
        if not snap:
            continue
        age_hours = (snap.captured_at - item.published_at).total_seconds() / 3600.0
        if age_hours <= 0:
            continue
        vph_values.append(float(snap.views) / age_hours)
        reactions_total = 0
        has_reactions = False
        if snap.likes is not None:
            reactions_total += int(snap.likes)
            has_reactions = True
        if snap.comments is not None:
            reactions_total += int(snap.comments)
            has_reactions = True
        if snap.shares is not None:
            reactions_total += int(snap.shares)
            has_reactions = True
        if has_reactions:
            rph_values.append(float(reactions_total) / age_hours)
        if snap.likes is not None and snap.comments is not None and snap.views > 0:
            er_values.append(float(snap.likes + snap.comments) / float(snap.views))

    if not vph_values:
        metrics = BaselineMetrics(
            vph_median=0.0,
            vph_iqr=1.0,
            er_median=None,
            er_iqr=None,
            n=0,
            rph_median=None,
            rph_iqr=None,
        )
    else:
        vph_med = median(vph_values)
        vph_i = max(iqr(vph_values), EPS)
        if er_values:
            er_med = median(er_values)
            er_i = max(iqr(er_values), EPS)
        else:
            er_med = None
            er_i = None
        if rph_values:
            rph_med = median(rph_values)
            rph_i = max(iqr(rph_values), EPS)
        else:
            rph_med = None
            rph_i = None
        metrics = BaselineMetrics(
            vph_median=vph_med,
            vph_iqr=vph_i,
            er_median=er_med,
            er_iqr=er_i,
            n=len(vph_values),
            rph_median=rph_med,
            rph_iqr=rph_i,
        )

    CompetitorBaseline.objects.create(
        competitor=competitor,
        computed_at=now,
        window_days=window_days,
        n_items=n_items,
        metrics={
            "vph_median": metrics.vph_median,
            "vph_iqr": metrics.vph_iqr,
            "er_median": metrics.er_median,
            "er_iqr": metrics.er_iqr,
            "n": metrics.n,
            "rph_median": metrics.rph_median,
            "rph_iqr": metrics.rph_iqr,
        },
    )
    return metrics


def score_items_for_period(
    *,
    items: list[ContentItem],
    competitor_by_item_id: dict[int, Competitor],
    baseline_by_competitor_id: dict[int, BaselineMetrics],
    period_start: datetime,
    period_end: datetime,
    adaptation_context: AdaptationContext | None = None,
    selection_mode: str = "strict",
) -> list[ScoredItem]:
    scored: list[ScoredItem] = []
    min_delta_views = int(getattr(settings, "MIN_DELTA_VIEWS", 500))
    min_views_end = int(getattr(settings, "MIN_VIEWS_END", 1000))
    short_window_fallback_hours = float(getattr(settings, "REPORT_SHORT_WINDOW_FALLBACK_HOURS", 6.0) or 6.0)
    # Hard floor to avoid noisy "viral" picks on very short periods (e.g. a few minutes).
    # The main threshold is scaled by period length below.
    min_delta_floor = 20
    max_age_days = int(getattr(settings, "REPORT_MAX_ITEM_AGE_DAYS", 60))
    min_published_at = period_end - timedelta(days=max_age_days)
    period_hours = max((period_end - period_start).total_seconds() / 3600.0, 0.0)

    for item in items:
        if item.platform == Platform.YOUTUBE and not _is_youtube_short_item(item):
            continue
        if item.published_at < min_published_at:
            continue
        competitor = competitor_by_item_id.get(item.id)
        if not competitor:
            continue
        baseline = baseline_by_competitor_id.get(competitor.id)
        if not baseline:
            continue
        adaptation_relevance_score, adaptation_relevance_factors = _compute_adaptation_relevance(
            item=item,
            competitor=competitor,
            adaptation_context=adaptation_context,
        )

        snap_end = (
            MetricSnapshot.objects.filter(content_item=item, captured_at__lte=period_end).order_by("-captured_at").first()
        )
        if not snap_end:
            continue

        views_end = int(snap_end.views)
        likes_end = int(snap_end.likes) if snap_end.likes is not None else None
        comments_end = int(snap_end.comments) if snap_end.comments is not None else None
        shares_end = int(snap_end.shares) if snap_end.shares is not None else None
        if views_end < min_views_end:
            continue

        # Main signal: delta views within the report period (views/hour).
        # If we don't have a start snapshot yet (warm-up), fall back to average views/hour since publish.
        snap_start = (
            MetricSnapshot.objects.filter(content_item=item, captured_at__lte=period_start).order_by("-captured_at").first()
        )

        delta_views: int | None = None
        delta_hours: float | None = None
        velocity: float | None = None
        score_type = "current_vph"
        fallback_reason: str | None = None

        if snap_start:
            dv = views_end - int(snap_start.views)
            dh = (snap_end.captured_at - snap_start.captured_at).total_seconds() / 3600.0
            if dv >= 0 and dh > 0:
                delta_views = dv
                delta_hours = dh
                # Scale the "minimal meaningful delta" by period length. MIN_DELTA_VIEWS is treated as a 24h threshold.
                effective_min_delta = max(int(min_delta_views * (dh / 24.0)), min_delta_floor)
                if dv < effective_min_delta and period_hours > short_window_fallback_hours:
                    if selection_mode != "fallback":
                        continue
                    fallback_allowed, fallback_reason = _fallback_candidate_passes(
                        adaptation_relevance_score=adaptation_relevance_score,
                        adaptation_relevance_factors=adaptation_relevance_factors,
                        delta_views=dv,
                        effective_min_delta=effective_min_delta,
                    )
                    if not fallback_allowed:
                        continue
                if dv >= effective_min_delta:
                    velocity = float(dv) / dh
                    score_type = "delta"
                elif fallback_reason is not None:
                    velocity = float(dv) / dh
                    score_type = "fallback_delta"

        if velocity is None:
            # Warm-up fallback: avoid tiny videos where vph is too noisy.
            if views_end < min_views_end:
                continue
            age_hours = (snap_end.captured_at - item.published_at).total_seconds() / 3600.0
            if age_hours <= 0:
                continue
            velocity = float(views_end) / age_hours

        if selection_mode == "fallback" and fallback_reason is None:
            continue

        er_end: float | None = None
        if snap_end.likes is not None and snap_end.comments is not None and snap_end.views > 0:
            er_end = float(snap_end.likes + snap_end.comments) / float(snap_end.views)

        z_vel = (velocity - baseline.vph_median) / max(baseline.vph_iqr, EPS)
        if er_end is not None and baseline.er_median is not None and baseline.er_iqr is not None:
            z_er = (er_end - baseline.er_median) / max(baseline.er_iqr, EPS)
            relative_score = 0.75 * z_vel + 0.25 * z_er
        else:
            relative_score = z_vel

        age_hours_end = max((snap_end.captured_at - item.published_at).total_seconds() / 3600.0, 0.0)
        views_signal = math.log10(max(float(views_end), 1.0))
        delta_signal = math.log10(max(float(delta_views or views_end), 1.0))
        baseline_vph = float(baseline.vph_median or 0.0)
        virality_ratio = float(velocity) / max(baseline_vph, EPS) if baseline_vph > 0 else 0.0
        recency_bonus = max(0.0, 1.0 - min(age_hours_end / float(max(max_age_days * 24, 1)), 1.0))
        base_score = (
            relative_score
            + (0.32 * delta_signal)
            + (0.18 * views_signal)
            + (0.20 * min(virality_ratio, 25.0) / 5.0)
            + (0.20 * recency_bonus)
        )
        adaptation_weight = float(getattr(settings, "ADAPTATION_RELEVANCE_WEIGHT", 2.0) or 2.0)
        score = base_score + (adaptation_weight * adaptation_relevance_score)

        scored.append(
            ScoredItem(
                content_item=item,
                competitor=competitor,
                views_end=views_end,
                likes_end=likes_end,
                comments_end=comments_end,
                shares_end=shares_end,
                velocity=velocity,
                score_type=score_type,
                delta_views=delta_views,
                delta_hours=delta_hours,
                er_end=er_end,
                base_score=base_score,
                adaptation_relevance_score=adaptation_relevance_score,
                adaptation_relevance_factors=adaptation_relevance_factors,
                selection_path="fallback" if fallback_reason else "strict",
                fallback_reason=fallback_reason,
                score=score,
            )
        )

    scored.sort(key=lambda s: s.score, reverse=True)
    return scored
