from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.test import override_settings

from tracking.models import Competitor, MetricSnapshot, Platform, TgUser, UserCompetitor
from tracking.services import report_pipeline
from tracking.services.report_pipeline import (
    ReportPipelineError,
    ReportPreview,
    assert_required_platform_sections,
    build_setup_verification_preview,
    create_and_send_setup_verification_report,
)


def test_assert_required_platform_sections_raises_for_empty_requested_platform():
    preview = ReportPreview(
        payload={},
        text="report",
        section_counts={"youtube": 0, "tiktok": 5, "instagram": 0},
    )

    with pytest.raises(
        ReportPipelineError,
        match="instagram verification produced an empty report section with the current provider data and scoring thresholds",
    ):
        assert_required_platform_sections(preview=preview, required_platforms={"instagram"})


@override_settings(MAX_COMPETITORS_PER_PLATFORM=20)
def test_build_setup_verification_preview_supports_up_to_sixty_entries(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=10, tg_chat_id=10, timezone_str="UTC")
    for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM):
        for idx in range(20):
            competitor = Competitor.objects.create(
                platform=platform,
                external_id=f"{platform}-{idx}",
                handle=f"{platform}_{idx}",
                url=(
                    f"https://www.instagram.com/reel/{idx}/"
                    if platform == Platform.INSTAGRAM
                    else f"https://example.com/{platform}/{idx}"
                ),
                display_name=f"{platform} {idx}",
            )
            UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)

    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)

    def fake_refresh(*, competitor, mode, captured_at, provider_fetch_cache=None):
        content_type = "short" if competitor.platform == Platform.YOUTUBE else "video"
        item = competitor.content_items.create(
            platform=competitor.platform,
            external_id=f"item-{competitor.external_id}",
            url=competitor.url,
            title="Latest #tag",
            description="desc",
            published_at=now - timedelta(hours=2),
            duration_seconds=30,
            meta={"content_type": content_type},
        )
        MetricSnapshot.objects.create(
            content_item=item,
            captured_at=now,
            views=1000,
            likes=100,
            comments=10,
            shares=5,
        )
        return [item]

    monkeypatch.setattr(report_pipeline, "refresh_competitor", fake_refresh)

    preview = build_setup_verification_preview(user=user, period_end=now)

    total_entries = sum(len(section.get("entries") or []) for section in preview.payload["sections"])
    assert total_entries == 60
    first_entry = preview.payload["sections"][0]["entries"][0]
    assert "avg_views_per_hour" in first_entry
    assert "avg_reactions_per_hour" in first_entry
    assert "avg_er" in first_entry
    assert "avg_virality" in first_entry
    assert set(first_entry["latest_item"]) == {
        "title",
        "url",
        "views",
        "avg_views_same_age",
        "views_delta_pct",
        "reactions",
        "avg_reactions_same_age",
        "reactions_delta_pct",
    }


def test_create_and_send_setup_verification_report_splits_messages(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=11, tg_chat_id=11, timezone_str="UTC")
    preview = ReportPreview(
        payload={"report_kind": "setup_verification", "sections": []},
        text="Проверка настройки завершена\n\n" + ("x" * 5000),
        section_counts={},
    )
    monkeypatch.setattr(report_pipeline, "build_setup_verification_preview", lambda **kwargs: preview)
    sent = []
    monkeypatch.setattr(report_pipeline, "send_message", lambda *, chat_id, text: sent.append(text) or {"message_id": len(sent)})

    result = create_and_send_setup_verification_report(
        user=user,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    assert len(sent) > 1
    assert result.telegram_result["message_ids"] == list(range(1, len(sent) + 1))
