from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from tracking.models import Platform
from tracking.services.reporting import (
    build_report_payload,
    build_setup_verification_payload,
    render_report_text,
    split_telegram_text,
)


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
        shares_end=5 if platform in {Platform.TIKTOK, Platform.INSTAGRAM} else None,
        velocity=250.0,
        score_type="delta",
        score=3.5,
        er_end=0.044,
        reactions_end=225,
        avg_views_same_age=3200,
        avg_reactions_same_age=140,
        views_delta_pct=56.25,
        reactions_delta_pct=60.7,
        virality=1.8,
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
    assert payload["sections"][0]["items"][0]["adaptation_relevance_score"] == 0.0
    assert payload["sections"][0]["items"][0]["adaptation_relevance_factors"] == {}


def test_build_report_payload_caps_items_per_platform_at_ten():
    scored = [_make_scored_item(platform=Platform.INSTAGRAM, suffix=str(idx)) for idx in range(12)]

    payload = build_report_payload(
        scored=scored,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    instagram_section = next(section for section in payload["sections"] if section["platform"] == Platform.INSTAGRAM)
    assert len(instagram_section["items"]) == 10


def test_build_report_payload_keeps_adaptation_relevance_diagnostics():
    scored_item = _make_scored_item(platform=Platform.YOUTUBE, suffix="8")
    scored_item.base_score = 3.1
    scored_item.adaptation_relevance_score = 0.42
    scored_item.adaptation_relevance_factors = {
        "niche_stem_overlap": 0.22,
        "instructional_markers": 0.20,
    }

    payload = build_report_payload(
        scored=[scored_item],
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    item_payload = payload["sections"][0]["items"][0]
    assert item_payload["base_score"] == 3.1
    assert item_payload["adaptation_relevance_score"] == 0.42
    assert item_payload["adaptation_relevance_factors"] == {
        "niche_stem_overlap": 0.22,
        "instructional_markers": 0.20,
    }


def test_render_report_text_preserves_youtube_section_and_stub_lines():
    payload = build_report_payload(
        scored=[_make_scored_item(platform=Platform.YOUTUBE, suffix="1")],
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    text = render_report_text(payload=payload, timezone_str="UTC")

    assert "YouTube:" in text
    assert "1) Comp 1" in text
    assert "Title 1" in text
    assert "Просмотры: 5000 vs 3200 (+56.2%)" in text
    assert "Реакции: 220 vs 140 (+60.7%)" in text
    assert "ER: 4.4%" in text
    assert "Вирусность: 1.8x" in text
    assert "TikTok:" in text
    assert "Нет подходящих роликов." in text
    assert "Instagram:" in text
    assert "Канал:" not in text
    assert "Опубликовано:" not in text


def test_render_report_text_renders_tiktok_items_with_share_counts():
    payload = build_report_payload(
        scored=[_make_scored_item(platform=Platform.TIKTOK, suffix="3")],
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    text = render_report_text(payload=payload, timezone_str="UTC")

    assert "TikTok:" in text
    assert "1) Comp 3" in text
    assert "Title 3" in text
    assert "Реакции: 225 vs 140 (+60.7%)" in text
    assert "https://example.com/3" in text


def test_render_report_text_renders_instagram_items():
    payload = build_report_payload(
        scored=[_make_scored_item(platform=Platform.INSTAGRAM, suffix="4")],
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    text = render_report_text(payload=payload, timezone_str="UTC")

    assert "Instagram:" in text
    assert "1) Comp 4" in text
    assert "Title 4" in text
    assert "ER: 4.4%" in text
    assert "https://example.com/4" in text


def test_render_report_text_truncates_regular_report_titles():
    scored_item = _make_scored_item(platform=Platform.YOUTUBE, suffix="9")
    scored_item.content_item.title = "A" * 120
    payload = build_report_payload(
        scored=[scored_item],
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    text = render_report_text(payload=payload, timezone_str="UTC")

    assert ("A" * 97) + "..." in text
    assert "A" * 120 not in text


def test_render_setup_verification_text_is_compact_and_strips_hashtags():
    payload = build_setup_verification_payload(
        generated_at=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
        sections=[
            {
                "platform": Platform.YOUTUBE,
                "entries": [
                    {
                        "competitor": {
                            "id": 1,
                            "display_name": "English with Anna",
                            "handle": "anna",
                            "external_id": "yt-1",
                            "url": "https://youtube.com/@anna",
                        },
                        "avg_views_per_hour": 123.4,
                        "avg_reactions_per_hour": 9.5,
                        "avg_er": 0.052,
                        "avg_virality": 1.08,
                        "latest_item": {
                            "title": "Lesson #english #teacher",
                            "url": "https://youtube.com/shorts/1",
                            "views": 2400,
                            "avg_views_same_age": 1800,
                            "views_delta_pct": 33.3,
                            "reactions": 120,
                            "avg_reactions_same_age": 90,
                            "reactions_delta_pct": 33.3,
                        },
                    }
                ],
            }
        ],
    )

    text = render_report_text(payload=payload, timezone_str="UTC")

    assert "Проверка настройки завершена" in text
    assert "English with Anna" in text
    assert "Среднее: 123.4 views/h | 9.5 reactions/h | ER 5.2% | virality 1.1x" in text
    assert "Последнее: Lesson" in text
    assert "Просмотры: 2400 vs 1800 среднее за тот же период (+33.3%)" in text
    assert "Реакции: 120 vs 90 среднее за тот же период (+33.3%)" in text
    assert text.count("Последнее:") == 1
    assert "#english" not in text
    assert "Источник:" not in text
    assert "Опубликовано:" not in text
    assert "Время: 2026-03-24 00:00" in text


def test_split_telegram_text_splits_long_setup_report_safely():
    block = "\n".join(
        [
            "1) Competitor",
            "https://example.com/account",
            "Среднее: 123.4 views/h | 9.5 reactions/h | ER 5.2% | virality 1.1x",
            "Последнее: Lesson",
            "https://example.com/item",
            "Просмотры: 2400 vs 1800 (+33.3%)",
            "Реакции: 120 vs 90 (+33.3%)",
        ]
    )
    text = "Проверка настройки завершена\n\n" + "\n\n".join(block for _ in range(60))

    chunks = split_telegram_text(text=text, max_len=1000)

    assert len(chunks) > 1
    assert all(len(chunk) <= 1000 for chunk in chunks)
