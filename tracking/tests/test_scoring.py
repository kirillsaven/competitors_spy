from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tracking.models import Competitor, ContentItem, MetricSnapshot, Platform, TgUser, UserCompetitor
from tracking.services.scoring import BaselineMetrics, score_items_for_period


@pytest.mark.django_db
def test_score_items_for_period_basic() -> None:
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
        meta={},
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
    assert scored[0].score == pytest.approx((120.0 - 50.0) / 10.0, rel=1e-6)


@pytest.mark.django_db
def test_score_items_for_period_skips_warmup_fallback_when_baseline_exists() -> None:
    user = TgUser.objects.create(tg_user_id=2, tg_chat_id=2, timezone_str="UTC+00:00")
    comp = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="UC999",
        display_name="Fallback Channel",
        meta={},
    )
    UserCompetitor.objects.create(user=user, competitor=comp, added_by="manual", is_active=True)
    published_at = datetime(2026, 2, 10, 0, 0, tzinfo=UTC)
    item = ContentItem.objects.create(
        competitor=comp,
        platform=Platform.YOUTUBE,
        external_id="vid2",
        url="https://www.youtube.com/watch?v=vid2",
        title="Video 2",
        description="",
        published_at=published_at,
        duration_seconds=60,
        meta={},
    )

    period_start = datetime(2026, 2, 11, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 2, 11, 5, 0, tzinfo=UTC)
    MetricSnapshot.objects.create(content_item=item, captured_at=period_end, views=1700, likes=30, comments=5, extra={})

    baseline = BaselineMetrics(vph_median=50.0, vph_iqr=10.0, er_median=None, er_iqr=None, n=4)
    scored = score_items_for_period(
        items=[item],
        competitor_by_item_id={item.id: comp},
        baseline_by_competitor_id={comp.id: baseline},
        period_start=period_start,
        period_end=period_end,
    )

    assert scored == []
