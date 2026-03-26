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
    assert scored[0].score > ((120.0 - 50.0) / 10.0)


@pytest.mark.django_db
def test_score_items_for_period_prefers_recent_high_scale_breakout(settings) -> None:
    settings.REPORT_MAX_ITEM_AGE_DAYS = 14
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
