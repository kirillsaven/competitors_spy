from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.test import override_settings

from tracking.models import Competitor, MetricSnapshot, Platform, Report, TgUser, UserCompetitor
from tracking.services import report_pipeline
from tracking.services.report_pipeline import (
    ReportPipelineError,
    ReportPreview,
    assert_required_platform_sections,
    build_setup_verification_preview,
    build_report_preview,
    create_and_send_report,
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


def test_create_and_send_report_splits_messages_and_marks_sent(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=111, tg_chat_id=111, timezone_str="UTC")
    preview = ReportPreview(
        payload={"sections": []},
        text="Обычный отчет\n\n" + ("x" * 5000),
        section_counts={},
    )
    monkeypatch.setattr(report_pipeline, "build_report_preview", lambda **kwargs: preview)
    sent = []
    monkeypatch.setattr(report_pipeline, "send_message", lambda *, chat_id, text: sent.append(text) or {"message_id": len(sent)})

    result = create_and_send_report(
        user=user,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    result.report.refresh_from_db()
    assert len(sent) > 1
    assert result.report.status == "sent"
    assert result.telegram_result["message_ids"] == list(range(1, len(sent) + 1))


def test_create_and_send_report_marks_failed_when_telegram_send_errors(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=112, tg_chat_id=112, timezone_str="UTC")
    preview = ReportPreview(
        payload={"sections": []},
        text="Обычный отчет",
        section_counts={},
    )
    monkeypatch.setattr(report_pipeline, "build_report_preview", lambda **kwargs: preview)

    def fail_send_message(**kwargs):
        raise RuntimeError("telegram failed")

    monkeypatch.setattr(report_pipeline, "send_message", fail_send_message)

    with pytest.raises(RuntimeError, match="telegram failed"):
        create_and_send_report(
            user=user,
            period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
            period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
        )

    report = Report.objects.latest("id")
    assert report.user_id == user.id
    assert report.status == "failed"


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
    monkeypatch.setattr(
        report_pipeline,
        "build_report_payload",
        lambda **kwargs: {"sections": [{"platform": Platform.TIKTOK, "items": [], "note": kwargs["platform_notes"][Platform.TIKTOK]}]},
    )
    monkeypatch.setattr(report_pipeline, "render_report_text", lambda **kwargs: "report")

    preview = build_report_preview(
        user=user,
        period_start=datetime(2026, 3, 24, 11, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 12, 0, tzinfo=UTC),
    )

    assert calls == {"prefetch": 1, "refresh": 1}
    assert preview.payload["sections"][0]["note"].startswith("Apify TikTok API error:")


def test_build_report_preview_keeps_youtube_section_when_tiktok_provider_fails(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=14, tg_chat_id=14, timezone_str="UTC")
    youtube = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="yt-1",
        handle="yt_handle",
        url="https://youtube.com/@yt_handle",
        display_name="YT Handle",
    )
    tiktok = Competitor.objects.create(
        platform=Platform.TIKTOK,
        external_id="tt-1",
        handle="tt_handle",
        url="https://www.tiktok.com/@tt_handle",
        display_name="TT Handle",
    )
    UserCompetitor.objects.create(user=user, competitor=youtube, is_active=True)
    UserCompetitor.objects.create(user=user, competitor=tiktok, is_active=True)

    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)

    def fake_refresh(*, competitor, mode, captured_at, provider_fetch_cache=None):
        if competitor.platform == Platform.TIKTOK:
            provider_fetch_cache.mark_platform_error(
                platform=Platform.TIKTOK,
                reason="Apify TikTok API error: status=403 body={'error': {'type': 'platform-feature-disabled'}}",
            )
            raise RuntimeError("Apify TikTok API error: status=403 body={'error': {'type': 'platform-feature-disabled'}}")
        item = competitor.content_items.create(
            platform=competitor.platform,
            external_id=f"item-{competitor.external_id}",
            url=competitor.url,
            title="Latest short",
            description="desc",
            published_at=now - timedelta(hours=2),
            duration_seconds=30,
            meta={"content_type": "short"},
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

    preview = build_report_preview(
        user=user,
        period_start=now - timedelta(hours=24),
        period_end=now,
    )

    sections = {section["platform"]: section for section in preview.payload["sections"]}
    assert len(sections[Platform.YOUTUBE]["items"]) == 1
    assert sections[Platform.TIKTOK]["items"] == []
    assert "platform-feature-disabled" in sections[Platform.TIKTOK]["note"]


def test_build_report_preview_keeps_youtube_section_when_instagram_prefetch_fails(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=141, tg_chat_id=141, timezone_str="UTC")
    youtube = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="yt-1",
        handle="yt_handle",
        url="https://youtube.com/@yt_handle",
        display_name="YT Handle",
    )
    instagram = Competitor.objects.create(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="ig_handle",
        url="https://www.instagram.com/ig_handle/",
        display_name="IG Handle",
    )
    UserCompetitor.objects.create(user=user, competitor=youtube, is_active=True)
    UserCompetitor.objects.create(user=user, competitor=instagram, is_active=True)

    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)

    def fake_fetch_instagram_profiles_cached(**kwargs):
        raise RuntimeError("Apify Instagram API error: status=403 body={'error': {'type': 'platform-feature-disabled'}}")

    def fake_refresh(*, competitor, mode, captured_at, provider_fetch_cache=None):
        if competitor.platform == Platform.INSTAGRAM:
            raise RuntimeError(provider_fetch_cache.get_platform_error(platform=Platform.INSTAGRAM))
        item = competitor.content_items.create(
            platform=competitor.platform,
            external_id=f"item-{competitor.external_id}",
            url=competitor.url,
            title="Latest short",
            description="desc",
            published_at=now - timedelta(hours=2),
            duration_seconds=30,
            meta={"content_type": "short"},
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

    monkeypatch.setattr(report_pipeline, "fetch_instagram_profiles_cached", fake_fetch_instagram_profiles_cached)
    monkeypatch.setattr(report_pipeline, "refresh_competitor", fake_refresh)

    preview = build_report_preview(
        user=user,
        period_start=now - timedelta(hours=24),
        period_end=now,
    )

    sections = {section["platform"]: section for section in preview.payload["sections"]}
    assert len(sections[Platform.YOUTUBE]["items"]) == 1
    assert sections[Platform.INSTAGRAM]["items"] == []
    assert "platform-feature-disabled" in sections[Platform.INSTAGRAM]["note"]


def test_build_report_preview_excludes_items_already_shown_in_regular_reports(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=15, tg_chat_id=15, timezone_str="UTC")
    competitor = Competitor.objects.create(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="teacher_ig",
        url="https://www.instagram.com/teacher_ig/",
        display_name="Teacher IG",
    )
    UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)
    Report.objects.create(
        user=user,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
        status="sent",
        payload={
            "sections": [
                {
                    "platform": Platform.INSTAGRAM,
                    "items": [
                        {
                            "platform": Platform.INSTAGRAM,
                            "video_id": "repeat-1",
                            "url": "https://www.instagram.com/reel/repeat-1/",
                        }
                    ],
                }
            ]
        },
    )
    Report.objects.create(
        user=user,
        period_start=datetime(2026, 3, 22, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        status="sent",
        payload={"report_kind": "setup_verification", "sections": []},
    )

    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)
    repeated_item = competitor.content_items.create(
        platform=Platform.INSTAGRAM,
        external_id="repeat-1",
        url="https://www.instagram.com/reel/repeat-1/",
        title="Repeated reel",
        description="desc",
        published_at=now - timedelta(hours=2),
        duration_seconds=30,
        meta={"content_type": "reel"},
    )
    fresh_item = competitor.content_items.create(
        platform=Platform.INSTAGRAM,
        external_id="fresh-1",
        url="https://www.instagram.com/reel/fresh-1/",
        title="Fresh reel",
        description="desc",
        published_at=now - timedelta(hours=1),
        duration_seconds=30,
        meta={"content_type": "reel"},
    )

    monkeypatch.setattr(report_pipeline, "_prefetch_provider_data_for_competitors", lambda **kwargs: None)
    monkeypatch.setattr(report_pipeline, "refresh_competitor", lambda **kwargs: [repeated_item, fresh_item])
    monkeypatch.setattr(
        report_pipeline,
        "compute_competitor_baseline",
        lambda **kwargs: SimpleNamespace(vph_median=100.0, rph_median=10.0),
    )
    monkeypatch.setattr(
        report_pipeline,
        "score_items_for_period",
        lambda **kwargs: [
            SimpleNamespace(
                content_item=repeated_item,
                competitor=competitor,
                views_end=5000,
                likes_end=200,
                comments_end=20,
                shares_end=5,
                velocity=250.0,
                score_type="delta",
                delta_views=1000,
                delta_hours=4.0,
                er_end=0.044,
                score=4.0,
            ),
            SimpleNamespace(
                content_item=fresh_item,
                competitor=competitor,
                views_end=7000,
                likes_end=300,
                comments_end=30,
                shares_end=7,
                velocity=350.0,
                score_type="delta",
                delta_views=1200,
                delta_hours=4.0,
                er_end=0.047,
                score=5.0,
            ),
        ],
    )
    monkeypatch.setattr(
        report_pipeline,
        "build_report_payload",
        lambda **kwargs: {
            "sections": [],
            "scored_external_ids": [item.content_item.external_id for item in kwargs["scored"]],
        },
    )
    monkeypatch.setattr(report_pipeline, "render_report_text", lambda **kwargs: "report")

    preview = build_report_preview(
        user=user,
        period_start=now - timedelta(hours=24),
        period_end=now,
    )

    assert preview.payload["scored_external_ids"] == ["fresh-1"]


def test_build_report_preview_excludes_stopword_matches_only_for_that_user(db, monkeypatch):
    user_blocked = TgUser.objects.create(tg_user_id=16, tg_chat_id=16, timezone_str="UTC", report_stopwords=["spoiler"])
    user_plain = TgUser.objects.create(tg_user_id=17, tg_chat_id=17, timezone_str="UTC")
    competitor = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="yt-stopword",
        handle="creator",
        url="https://www.youtube.com/@creator",
        display_name="Creator",
    )
    UserCompetitor.objects.create(user=user_blocked, competitor=competitor, is_active=True)
    UserCompetitor.objects.create(user=user_plain, competitor=competitor, is_active=True)
    item = competitor.content_items.create(
        platform=Platform.YOUTUBE,
        external_id="stopword-item",
        url="https://www.youtube.com/watch?v=stopword-item",
        title="Major Spoiler video",
        description="desc",
        published_at=datetime(2026, 3, 24, 11, 0, tzinfo=UTC),
        duration_seconds=30,
        meta={"content_type": "short"},
    )

    scored_item = SimpleNamespace(
        content_item=item,
        competitor=competitor,
        views_end=5000,
        likes_end=100,
        comments_end=10,
        shares_end=1,
        velocity=250.0,
        score_type="delta",
        delta_views=1000,
        delta_hours=4.0,
        er_end=0.022,
        score=3.0,
    )
    monkeypatch.setattr(report_pipeline, "refresh_competitor", lambda **kwargs: [item])
    monkeypatch.setattr(report_pipeline, "compute_competitor_baseline", lambda **kwargs: SimpleNamespace(vph_median=1.0, rph_median=1.0))
    monkeypatch.setattr(report_pipeline, "score_items_for_period", lambda **kwargs: [scored_item])
    monkeypatch.setattr(
        report_pipeline,
        "build_report_payload",
        lambda **kwargs: {
            "sections": [],
            "scored_external_ids": [entry.content_item.external_id for entry in kwargs["scored"]],
        },
    )
    monkeypatch.setattr(report_pipeline, "render_report_text", lambda **kwargs: "report")

    blocked_preview = build_report_preview(
        user=user_blocked,
        period_start=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 12, 0, tzinfo=UTC),
    )
    plain_preview = build_report_preview(
        user=user_plain,
        period_start=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 12, 0, tzinfo=UTC),
    )

    assert blocked_preview.payload["scored_external_ids"] == []
    assert plain_preview.payload["scored_external_ids"] == ["stopword-item"]


def test_build_setup_verification_preview_uses_latest_visible_item_when_newest_is_blocked(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=18, tg_chat_id=18, timezone_str="UTC", report_stopwords=["spoiler"])
    competitor = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="yt-setup-stopword",
        handle="creator",
        url="https://www.youtube.com/@creator",
        display_name="Creator",
    )
    UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)
    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)

    def fake_refresh(*, competitor, mode, captured_at, provider_fetch_cache=None):
        visible = competitor.content_items.create(
            platform=competitor.platform,
            external_id="visible-item",
            url="https://www.youtube.com/watch?v=visible-item",
            title="Visible short",
            description="desc",
            published_at=now - timedelta(hours=3),
            duration_seconds=30,
            meta={"content_type": "short"},
        )
        blocked = competitor.content_items.create(
            platform=competitor.platform,
            external_id="blocked-item",
            url="https://www.youtube.com/watch?v=blocked-item",
            title="Big spoiler short",
            description="desc",
            published_at=now - timedelta(hours=1),
            duration_seconds=30,
            meta={"content_type": "short"},
        )
        for item, views in ((visible, 1000), (blocked, 2000)):
            MetricSnapshot.objects.create(
                content_item=item,
                captured_at=now,
                views=views,
                likes=100,
                comments=10,
                shares=5,
            )
        return [visible, blocked]

    monkeypatch.setattr(report_pipeline, "refresh_competitor", fake_refresh)

    preview = build_setup_verification_preview(user=user, period_end=now)

    first_entry = preview.payload["sections"][0]["entries"][0]
    assert first_entry["latest_item"]["title"] == "Visible short"


def test_build_setup_verification_preview_reports_when_all_short_items_are_hidden_by_stopwords(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=19, tg_chat_id=19, timezone_str="UTC", report_stopwords=["spoiler"])
    competitor = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="yt-setup-hidden",
        handle="creator",
        url="https://www.youtube.com/@creator",
        display_name="Creator",
    )
    UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)
    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)

    def fake_refresh(*, competitor, mode, captured_at, provider_fetch_cache=None):
        item = competitor.content_items.create(
            platform=competitor.platform,
            external_id="blocked-only-item",
            url="https://www.youtube.com/watch?v=blocked-only-item",
            title="Only spoiler short",
            description="desc",
            published_at=now - timedelta(hours=1),
            duration_seconds=30,
            meta={"content_type": "short"},
        )
        MetricSnapshot.objects.create(
            content_item=item,
            captured_at=now,
            views=2000,
            likes=100,
            comments=10,
            shares=5,
        )
        return [item]

    monkeypatch.setattr(report_pipeline, "refresh_competitor", fake_refresh)

    preview = build_setup_verification_preview(user=user, period_end=now)

    first_entry = preview.payload["sections"][0]["entries"][0]
    assert first_entry["reason"] == "все подходящие короткие видео скрыты стоп-словами пользователя"
