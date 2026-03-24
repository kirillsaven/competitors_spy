from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from tracking.models import Platform
from tracking.services.reporting import build_report_payload, render_report_text


def _make_scored_item(*, platform: str, suffix: str):
    competitor = SimpleNamespace(
        id=int(suffix),
        display_name=f"Comp {suffix}",
        handle=f"handle{suffix}",
        external_id=f"comp-{suffix}",
    )
    content_item = SimpleNamespace(
        platform=platform,
        external_id=f"vid-{suffix}",
        title=f"Title {suffix}",
        url=f"https://example.com/{suffix}",
        published_at=datetime(2026, 3, 24, 12, 0, tzinfo=UTC),
        meta={"content_type": "short" if suffix == "1" else "video"},
    )
    return SimpleNamespace(
        content_item=content_item,
        competitor=competitor,
        delta_views=1000,
        delta_hours=4.0,
        views_end=5000,
        likes_end=200,
        comments_end=20,
        velocity=250.0,
        score_type="delta",
        score=3.5,
        er_end=0.044,
    )


def test_build_report_payload_groups_by_platform_and_keeps_stub_sections():
    scored = [
        _make_scored_item(platform=Platform.YOUTUBE, suffix="1"),
        _make_scored_item(platform=Platform.YOUTUBE, suffix="2"),
    ]

    payload = build_report_payload(
        scored=scored,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    assert [section["platform"] for section in payload["sections"]] == [
        Platform.YOUTUBE,
        Platform.TIKTOK,
        Platform.INSTAGRAM,
    ]
    assert len(payload["sections"][0]["items"]) == 2
    assert payload["sections"][1]["items"] == []
    assert payload["sections"][2]["items"] == []


def test_render_report_text_preserves_youtube_section_and_stub_lines():
    payload = build_report_payload(
        scored=[_make_scored_item(platform=Platform.YOUTUBE, suffix="1")],
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    text = render_report_text(payload=payload, timezone_str="UTC")

    assert "YouTube:" in text
    assert "Title 1 [Shorts]" in text
    assert "TikTok: MVP: пока не поддерживается" in text
    assert "Instagram: MVP: пока не поддерживается" in text
