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
    build_report_preview,
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


def test_build_setup_verification_preview_prefetches_ig_and_tt_provider_payloads(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=12, tg_chat_id=12, timezone_str="UTC")
    instagram = Competitor.objects.create(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="teacher_ig",
        url="https://www.instagram.com/teacher_ig/",
        display_name="Teacher IG",
    )
    tiktok = Competitor.objects.create(
        platform=Platform.TIKTOK,
        external_id="tt-1",
        handle="teacher_tt",
        url="https://www.tiktok.com/@teacher_tt",
        display_name="Teacher TT",
    )
    UserCompetitor.objects.create(user=user, competitor=instagram, is_active=True)
    UserCompetitor.objects.create(user=user, competitor=tiktok, is_active=True)

    seen = {"instagram": 0, "tiktok": 0}

    monkeypatch.setattr(
        report_pipeline,
        "fetch_instagram_profiles_cached",
        lambda **kwargs: [
            {
                "id": "ig-1",
                "username": "teacher_ig",
                "url": "https://www.instagram.com/teacher_ig/",
            }
        ],
    )
    monkeypatch.setattr(
        report_pipeline,
        "fetch_tiktok_profile_feeds_cached",
        lambda **kwargs: {"teacher_tt": [{"id": "tt-item"}]},
    )

    def fake_refresh(*, competitor, mode, captured_at, provider_fetch_cache=None):
        if competitor.platform == Platform.INSTAGRAM:
            seen["instagram"] += 1
            assert provider_fetch_cache.get_instagram_profile(lookup="teacher_ig")["id"] == "ig-1"
        if competitor.platform == Platform.TIKTOK:
            seen["tiktok"] += 1
            assert provider_fetch_cache.get_tiktok_feed(handle="teacher_tt") == [{"id": "tt-item"}]
        item = competitor.content_items.create(
            platform=competitor.platform,
            external_id=f"item-{competitor.external_id}",
            url=competitor.url,
            title="Latest item",
            description="desc",
            published_at=datetime(2026, 3, 24, 10, 0, tzinfo=UTC),
            duration_seconds=30,
            meta={"content_type": "reel" if competitor.platform == Platform.INSTAGRAM else "video"},
        )
        MetricSnapshot.objects.create(
            content_item=item,
            captured_at=datetime(2026, 3, 24, 12, 0, tzinfo=UTC),
            views=100,
            likes=10,
            comments=1,
            shares=1,
        )
        return [item]

    monkeypatch.setattr(report_pipeline, "refresh_competitor", fake_refresh)

    preview = build_setup_verification_preview(
        user=user,
        period_end=datetime(2026, 3, 24, 12, 0, tzinfo=UTC),
    )

    assert preview.section_counts[Platform.INSTAGRAM] == 1
    assert preview.section_counts[Platform.TIKTOK] == 1
    assert seen == {"instagram": 1, "tiktok": 1}


def test_build_report_preview_marks_provider_platform_error_once(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=13, tg_chat_id=13, timezone_str="UTC")
    first = Competitor.objects.create(
        platform=Platform.TIKTOK,
        external_id="tt-1",
        handle="teacher_tt",
        url="https://www.tiktok.com/@teacher_tt",
        display_name="Teacher TT",
    )
    second = Competitor.objects.create(
        platform=Platform.TIKTOK,
        external_id="tt-2",
        handle="teacher_tt_2",
        url="https://www.tiktok.com/@teacher_tt_2",
        display_name="Teacher TT 2",
    )
    UserCompetitor.objects.create(user=user, competitor=first, is_active=True)
    UserCompetitor.objects.create(user=user, competitor=second, is_active=True)

    calls = {"prefetch": 0, "refresh": 0}

    def fake_prefetch(**kwargs):
        calls["prefetch"] += 1
        kwargs["provider_fetch_cache"].mark_platform_error(
            platform=Platform.TIKTOK,
            reason="Apify TikTok API error: status=403 body={'error': {'type': 'platform-feature-disabled'}}",
        )

    def fake_refresh(*, competitor, mode, captured_at, provider_fetch_cache=None):
        calls["refresh"] += 1
        raise RuntimeError(provider_fetch_cache.get_platform_error(platform=Platform.TIKTOK))

    monkeypatch.setattr(report_pipeline, "_prefetch_provider_data_for_competitors", fake_prefetch)
    monkeypatch.setattr(report_pipeline, "refresh_competitor", fake_refresh)
    monkeypatch.setattr(report_pipeline, "build_report_payload", lambda **kwargs: {"sections": []})
    monkeypatch.setattr(report_pipeline, "render_report_text", lambda **kwargs: "report")

    with pytest.raises(RuntimeError, match="platform-feature-disabled"):
        build_report_preview(
            user=user,
            period_start=datetime(2026, 3, 24, 11, 0, tzinfo=UTC),
            period_end=datetime(2026, 3, 24, 12, 0, tzinfo=UTC),
        )

    assert calls == {"prefetch": 1, "refresh": 1}
