from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from django.conf import settings

from common.stats import iqr, median

from tracking.models import Competitor, CompetitorBaseline, ContentItem, MetricSnapshot


EPS = 1e-6


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
    score: float


def compute_competitor_baseline(*, competitor: Competitor, now: datetime) -> BaselineMetrics:
    window_days = int(getattr(settings, "BASELINE_WINDOW_DAYS", 30))
    n_items = int(getattr(settings, "BASELINE_N", 30))
    window_start = now - timedelta(days=window_days)

    items = (
        ContentItem.objects.filter(competitor=competitor, published_at__gte=window_start)
        .order_by("-published_at")
        .all()[:n_items]
    )

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
) -> list[ScoredItem]:
    scored: list[ScoredItem] = []
    min_delta_views = int(getattr(settings, "MIN_DELTA_VIEWS", 500))
    min_views_end = int(getattr(settings, "MIN_VIEWS_END", 1000))
    # Hard floor to avoid noisy "viral" picks on very short periods (e.g. a few minutes).
    # The main threshold is scaled by period length below.
    min_delta_floor = 20
    max_age_days = int(getattr(settings, "REPORT_MAX_ITEM_AGE_DAYS", 14))
    min_published_at = period_end - timedelta(days=max_age_days)

    for item in items:
        if item.published_at < min_published_at:
            continue
        competitor = competitor_by_item_id.get(item.id)
        if not competitor:
            continue
        baseline = baseline_by_competitor_id.get(competitor.id)
        if not baseline:
            continue

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

        if snap_start:
            dv = views_end - int(snap_start.views)
            dh = (snap_end.captured_at - snap_start.captured_at).total_seconds() / 3600.0
            if dv >= 0 and dh > 0:
                delta_views = dv
                delta_hours = dh
                # Scale the "minimal meaningful delta" by period length. MIN_DELTA_VIEWS is treated as a 24h threshold.
                effective_min_delta = max(int(min_delta_views * (dh / 24.0)), min_delta_floor)
                if dv < effective_min_delta:
                    continue
                velocity = float(dv) / dh
                score_type = "delta"

        if velocity is None:
            # Warm-up fallback: avoid tiny videos where vph is too noisy.
            if views_end < min_views_end:
                continue
            age_hours = (snap_end.captured_at - item.published_at).total_seconds() / 3600.0
            if age_hours <= 0:
                continue
            velocity = float(views_end) / age_hours

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
        score = (
            relative_score
            + (0.32 * delta_signal)
            + (0.18 * views_signal)
            + (0.20 * min(virality_ratio, 25.0) / 5.0)
            + (0.20 * recency_bonus)
        )

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
                score=score,
            )
        )

    scored.sort(key=lambda s: s.score, reverse=True)
    return scored
