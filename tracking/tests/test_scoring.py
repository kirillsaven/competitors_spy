from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tracking.models import Competitor, ContentItem, MetricSnapshot, Platform, TgUser, UserCompetitor
from tracking.services.scoring import BaselineMetrics, compute_competitor_baseline, score_items_for_period


@pytest.mark.django_db
def test_score_items_for_period_basic(settings) -> None:
    settings.MIN_VIEWS_END = 0
    user = TgUser.objects.create(tg_user_id=1, tg_chat_id=1, timezone_str="UTC+00:00")
    comp = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="UC123",
        display_name="Test Channel",
        meta={},
    )
    UserCompetitor.objects.create(user=user, competitor=comp, added_by="manual", is_active=True)
    published_at = datetime(2026, 2, 10, 0, 0, tzinfo=UTC)
    item = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.YOUTUBE,
        external_id="vid1",
        url="https://www.youtube.com/watch?v=vid1",
        title="Video",
        description="",
        published_at=published_at,
        duration_seconds=60,
        meta={"content_type": "short"},
    )

    period_start = datetime(2026, 2, 11, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 2, 11, 5, 0, tzinfo=UTC)
    MetricSnapshot.objects.create(content_item=item, captured_at=period_start, views=100, likes=10, comments=2, extra={})
    MetricSnapshot.objects.create(content_item=item, captured_at=period_end, views=700, likes=30, comments=5, extra={})

    baseline = BaselineMetrics(vph_median=50.0, vph_iqr=10.0, er_median=None, er_iqr=None, n=4)
    scored = score_items_for_period(
        items=[item],
        competitor_by_item_id={item.id: comp},
        baseline_by_competitor_id={comp.id: baseline},
        period_start=period_start,
        period_end=period_end,
    )
    assert len(scored) == 1
    assert scored[0].delta_views == 600
    assert scored[0].score > ((120.0 - 50.0) / 10.0)


@pytest.mark.django_db
def test_score_items_for_period_prefers_recent_high_scale_breakout(settings) -> None:
    settings.REPORT_MAX_ITEM_AGE_DAYS = 14
    settings.MIN_VIEWS_END = 1000
    user = TgUser.objects.create(tg_user_id=2, tg_chat_id=2, timezone_str="UTC+00:00")
    comp = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="UC999",
        display_name="Scale Channel",
        meta={},
    )
    UserCompetitor.objects.create(user=user, competitor=comp, added_by="manual", is_active=True)

    period_start = datetime(2026, 2, 11, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 2, 11, 5, 0, tzinfo=UTC)

    small = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.YOUTUBE,
        external_id="small",
        url="https://www.youtube.com/watch?v=small",
        title="Small breakout",
        description="",
        published_at=period_end - timedelta(hours=10),
        duration_seconds=55,
        meta={"content_type": "short"},
    )
    large = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.YOUTUBE,
        external_id="large",
        url="https://www.youtube.com/watch?v=large",
        title="Large breakout",
        description="",
        published_at=period_end - timedelta(hours=8),
        duration_seconds=55,
        meta={"content_type": "short"},
    )

    MetricSnapshot.objects.create(content_item=small, captured_at=period_start, views=100, likes=5, comments=1, extra={})
    MetricSnapshot.objects.create(content_item=small, captured_at=period_end, views=1800, likes=60, comments=10, extra={})
    MetricSnapshot.objects.create(content_item=large, captured_at=period_start, views=5000, likes=120, comments=18, extra={})
    MetricSnapshot.objects.create(content_item=large, captured_at=period_end, views=48000, likes=1400, comments=160, extra={})

    baseline = BaselineMetrics(vph_median=120.0, vph_iqr=25.0, er_median=0.02, er_iqr=0.01, n=10, rph_median=4.0, rph_iqr=1.0)
    scored = score_items_for_period(
        items=[small, large],
        competitor_by_item_id={small.id: comp, large.id: comp},
        baseline_by_competitor_id={comp.id: baseline},
        period_start=period_start,
        period_end=period_end,
    )

    assert [item.content_item.external_id for item in scored] == ["large", "small"]


@pytest.mark.django_db
def test_score_items_for_period_keeps_items_within_60_day_candidate_window(settings) -> None:
    settings.REPORT_MAX_ITEM_AGE_DAYS = 60
    settings.MIN_VIEWS_END = 1000
    comp = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="UC-60D-KEEP",
        display_name="60d Keep",
        meta={},
    )

    period_start = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 3, 30, 0, 0, tzinfo=UTC)
    item = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.YOUTUBE,
        external_id="within-60d",
        url="https://www.youtube.com/watch?v=within-60d",
        title="Within 60d",
        description="",
        published_at=period_end - timedelta(days=30),
        duration_seconds=55,
        meta={"content_type": "short"},
    )
    MetricSnapshot.objects.create(content_item=item, captured_at=period_start, views=2000, likes=50, comments=5, extra={})
    MetricSnapshot.objects.create(content_item=item, captured_at=period_end, views=6000, likes=150, comments=15, extra={})

    baseline = BaselineMetrics(vph_median=100.0, vph_iqr=20.0, er_median=0.02, er_iqr=0.01, n=10, rph_median=3.0, rph_iqr=1.0)
    scored = score_items_for_period(
        items=[item],
        competitor_by_item_id={item.id: comp},
        baseline_by_competitor_id={comp.id: baseline},
        period_start=period_start,
        period_end=period_end,
    )

    assert [entry.content_item.external_id for entry in scored] == ["within-60d"]


@pytest.mark.django_db
def test_score_items_for_period_filters_items_older_than_60_day_candidate_window(settings) -> None:
    settings.REPORT_MAX_ITEM_AGE_DAYS = 60
    settings.MIN_VIEWS_END = 1000
    comp = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="UC-60D-DROP",
        display_name="60d Drop",
        meta={},
    )

    period_start = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 3, 30, 0, 0, tzinfo=UTC)
    item = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.YOUTUBE,
        external_id="older-than-60d",
        url="https://www.youtube.com/watch?v=older-than-60d",
        title="Older than 60d",
        description="",
        published_at=period_end - timedelta(days=61),
        duration_seconds=55,
        meta={"content_type": "short"},
    )
    MetricSnapshot.objects.create(content_item=item, captured_at=period_start, views=2000, likes=50, comments=5, extra={})
    MetricSnapshot.objects.create(content_item=item, captured_at=period_end, views=6000, likes=150, comments=15, extra={})

    baseline = BaselineMetrics(vph_median=100.0, vph_iqr=20.0, er_median=0.02, er_iqr=0.01, n=10, rph_median=3.0, rph_iqr=1.0)
    scored = score_items_for_period(
        items=[item],
        competitor_by_item_id={item.id: comp},
        baseline_by_competitor_id={comp.id: baseline},
        period_start=period_start,
        period_end=period_end,
    )

    assert scored == []


@pytest.mark.django_db
def test_score_items_for_period_recency_bonus_prefers_fresher_item_when_other_signals_match(settings) -> None:
    settings.REPORT_MAX_ITEM_AGE_DAYS = 60
    settings.MIN_VIEWS_END = 1000
    comp = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="UC-RECENCY",
        display_name="Recency Channel",
        meta={},
    )

    period_start = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 3, 30, 0, 0, tzinfo=UTC)
    fresh = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.YOUTUBE,
        external_id="fresh-5d",
        url="https://www.youtube.com/watch?v=fresh-5d",
        title="Fresh 5d",
        description="",
        published_at=period_end - timedelta(days=5),
        duration_seconds=55,
        meta={"content_type": "short"},
    )
    older = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.YOUTUBE,
        external_id="older-30d",
        url="https://www.youtube.com/watch?v=older-30d",
        title="Older 30d",
        description="",
        published_at=period_end - timedelta(days=30),
        duration_seconds=55,
        meta={"content_type": "short"},
    )
    for item in (fresh, older):
        MetricSnapshot.objects.create(content_item=item, captured_at=period_start, views=2000, likes=50, comments=5, extra={})
        MetricSnapshot.objects.create(content_item=item, captured_at=period_end, views=6000, likes=150, comments=15, extra={})

    baseline = BaselineMetrics(vph_median=100.0, vph_iqr=20.0, er_median=0.02, er_iqr=0.01, n=10, rph_median=3.0, rph_iqr=1.0)
    scored = score_items_for_period(
        items=[older, fresh],
        competitor_by_item_id={fresh.id: comp, older.id: comp},
        baseline_by_competitor_id={comp.id: baseline},
        period_start=period_start,
        period_end=period_end,
    )

    assert [entry.content_item.external_id for entry in scored] == ["fresh-5d", "older-30d"]


@pytest.mark.django_db
def test_score_items_for_period_filters_low_total_views_even_with_delta(settings) -> None:
    settings.MIN_VIEWS_END = 1000
    user = TgUser.objects.create(tg_user_id=3, tg_chat_id=3, timezone_str="UTC+00:00")
    comp = Competitor.objects.create(platform=Platform.YOUTUBE, external_id="UC777", display_name="Low Views", meta={})
    UserCompetitor.objects.create(user=user, competitor=comp, added_by="manual", is_active=True)

    published_at = datetime(2026, 2, 10, 23, 0, tzinfo=UTC)
    item = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.YOUTUBE,
        external_id="tiny",
        url="https://www.youtube.com/watch?v=tiny",
        title="Tiny breakout",
        description="",
        published_at=published_at,
        duration_seconds=55,
        meta={"content_type": "short"},
    )
    period_start = datetime(2026, 2, 11, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 2, 11, 5, 0, tzinfo=UTC)
    MetricSnapshot.objects.create(content_item=item, captured_at=period_start, views=40, likes=5, comments=1, extra={})
    MetricSnapshot.objects.create(content_item=item, captured_at=period_end, views=900, likes=50, comments=10, extra={})

    baseline = BaselineMetrics(vph_median=20.0, vph_iqr=5.0, er_median=0.02, er_iqr=0.01, n=10, rph_median=1.0, rph_iqr=0.5)
    scored = score_items_for_period(
        items=[item],
        competitor_by_item_id={item.id: comp},
        baseline_by_competitor_id={comp.id: baseline},
        period_start=period_start,
        period_end=period_end,
    )

    assert scored == []


@pytest.mark.django_db
def test_score_items_for_period_uses_current_vph_fallback_for_short_first_window(settings) -> None:
    settings.MIN_VIEWS_END = 1000
    settings.MIN_DELTA_VIEWS = 500
    settings.REPORT_SHORT_WINDOW_FALLBACK_HOURS = 6
    user = TgUser.objects.create(tg_user_id=4, tg_chat_id=4, timezone_str="UTC+00:00")
    comp = Competitor.objects.create(platform=Platform.INSTAGRAM, external_id="IG100", display_name="IG Warmup", meta={})
    UserCompetitor.objects.create(user=user, competitor=comp, added_by="manual", is_active=True)

    published_at = datetime(2026, 3, 27, 10, 0, tzinfo=UTC)
    item = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.INSTAGRAM,
        external_id="reel-1",
        url="https://www.instagram.com/reel/reel-1/",
        title="Warmup reel",
        description="",
        published_at=published_at,
        duration_seconds=30,
        meta={"content_type": "reel"},
    )
    period_start = datetime(2026, 3, 28, 13, 35, 42, tzinfo=UTC)
    period_end = datetime(2026, 3, 28, 13, 38, 11, tzinfo=UTC)
    MetricSnapshot.objects.create(content_item=item, captured_at=period_start, views=2200, likes=120, comments=8, extra={})
    MetricSnapshot.objects.create(content_item=item, captured_at=period_end, views=2200, likes=120, comments=8, extra={})

    baseline = BaselineMetrics(vph_median=20.0, vph_iqr=5.0, er_median=0.02, er_iqr=0.01, n=10, rph_median=1.0, rph_iqr=0.5)
    scored = score_items_for_period(
        items=[item],
        competitor_by_item_id={item.id: comp},
        baseline_by_competitor_id={comp.id: baseline},
        period_start=period_start,
        period_end=period_end,
    )

    assert len(scored) == 1
    assert scored[0].score_type == "current_vph"
    assert scored[0].delta_views == 0


@pytest.mark.django_db
def test_score_items_for_period_skips_youtube_long_form_items(settings) -> None:
    settings.MIN_VIEWS_END = 0
    comp = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="UC-LONG",
        display_name="Long Channel",
        meta={},
    )
    short_item = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.YOUTUBE,
        external_id="yt-short",
        url="https://www.youtube.com/watch?v=yt-short",
        title="Short breakout",
        description="",
        published_at=datetime(2026, 2, 10, 0, 0, tzinfo=UTC),
        duration_seconds=45,
        meta={"content_type": "short"},
    )
    long_item = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.YOUTUBE,
        external_id="yt-long",
        url="https://www.youtube.com/watch?v=yt-long",
        title="Long breakout",
        description="",
        published_at=datetime(2026, 2, 10, 1, 0, tzinfo=UTC),
        duration_seconds=480,
        meta={"content_type": "video"},
    )
    period_start = datetime(2026, 2, 11, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 2, 11, 5, 0, tzinfo=UTC)
    MetricSnapshot.objects.create(content_item=short_item, captured_at=period_start, views=100, likes=10, comments=2, extra={})
    MetricSnapshot.objects.create(content_item=short_item, captured_at=period_end, views=700, likes=30, comments=5, extra={})
    MetricSnapshot.objects.create(content_item=long_item, captured_at=period_start, views=1000, likes=50, comments=6, extra={})
    MetricSnapshot.objects.create(content_item=long_item, captured_at=period_end, views=9000, likes=400, comments=40, extra={})

    baseline = BaselineMetrics(vph_median=50.0, vph_iqr=10.0, er_median=None, er_iqr=None, n=4)
    scored = score_items_for_period(
        items=[short_item, long_item],
        competitor_by_item_id={short_item.id: comp, long_item.id: comp},
        baseline_by_competitor_id={comp.id: baseline},
        period_start=period_start,
        period_end=period_end,
    )

    assert [item.content_item.external_id for item in scored] == ["yt-short"]


@pytest.mark.django_db
def test_compute_competitor_baseline_uses_youtube_shorts_only() -> None:
    comp = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="UC-BASELINE",
        display_name="Baseline Channel",
        meta={},
    )
    now = datetime(2026, 2, 11, 5, 0, tzinfo=UTC)
    short_item = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.YOUTUBE,
        external_id="baseline-short",
        url="https://www.youtube.com/watch?v=baseline-short",
        title="Short baseline",
        description="",
        published_at=now - timedelta(hours=5),
        duration_seconds=40,
        meta={"content_type": "short"},
    )
    long_item = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.YOUTUBE,
        external_id="baseline-long",
        url="https://www.youtube.com/watch?v=baseline-long",
        title="Long baseline",
        description="",
        published_at=now - timedelta(hours=5),
        duration_seconds=600,
        meta={"content_type": "video"},
    )
    MetricSnapshot.objects.create(content_item=short_item, captured_at=now, views=1000, likes=100, comments=20, extra={})
    MetricSnapshot.objects.create(content_item=long_item, captured_at=now, views=100000, likes=1000, comments=200, extra={})

    baseline = compute_competitor_baseline(competitor=comp, now=now)

    assert baseline.n == 1
    assert baseline.vph_median == 200.0
