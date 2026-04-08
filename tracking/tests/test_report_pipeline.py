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


def test_create_and_send_report_sends_youtube_suggestion_message_when_present(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=113, tg_chat_id=113, timezone_str="UTC")
    preview = ReportPreview(
        payload={
            "sections": [],
            "suggested_competitors": {
                "youtube": {
                    "items": [
                        {
                            "channel_id": "chan-1",
                            "channel_title": "Topic Coach",
                            "appearance_count": 2,
                        }
                    ]
                }
            },
        },
        text="Обычный отчет",
        section_counts={},
    )
    monkeypatch.setattr(report_pipeline, "build_report_preview", lambda **kwargs: preview)
    sent: list[dict] = []

    def fake_send_message(**kwargs):
        sent.append(kwargs)
        return {"message_id": len(sent)}

    monkeypatch.setattr(report_pipeline, "send_message", fake_send_message)

    result = create_and_send_report(
        user=user,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    result.report.refresh_from_db()
    assert len(sent) == 2
    assert sent[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"suggytadd:{result.report.id}:0"
    assert result.telegram_result["suggestion_message_id"] == 2
    assert result.report.payload["suggested_competitors"]["youtube"]["diagnostics"]["suggestions_sent"] == 1
    assert result.report.payload["suggested_competitors"]["youtube"]["delivery"]["sent_items"][0]["channel_id"] == "chan-1"
    assert result.report.payload["suggested_competitors"]["youtube"]["diagnostics"]["suppressed_by_run_cap"] == 0
    assert result.report.payload["suggested_competitors"]["youtube"]["diagnostics"]["suppressed_by_cooldown"] == 0


def test_create_and_send_report_skips_youtube_suggestion_message_when_none(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=114, tg_chat_id=114, timezone_str="UTC")
    preview = ReportPreview(
        payload={"sections": [], "suggested_competitors": {"youtube": {"items": []}}},
        text="Обычный отчет",
        section_counts={},
    )
    monkeypatch.setattr(report_pipeline, "build_report_preview", lambda **kwargs: preview)
    sent: list[dict] = []
    monkeypatch.setattr(report_pipeline, "send_message", lambda **kwargs: sent.append(kwargs) or {"message_id": len(sent)})

    result = create_and_send_report(
        user=user,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    result.report.refresh_from_db()
    assert len(sent) == 1
    assert "suggestion_message_id" not in result.telegram_result
    assert result.report.payload["suggested_competitors"]["youtube"]["diagnostics"]["suggestions_sent"] == 0


def test_create_and_send_report_sends_instagram_suggestion_message_when_present(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=1142, tg_chat_id=1142, timezone_str="UTC")
    preview = ReportPreview(
        payload={
            "sections": [],
            "suggested_competitors": {
                "youtube": {"items": []},
                "instagram": {
                    "items": [
                        {
                            "competitor_external_id": "ig-1",
                            "competitor_display_name": "IG Coach",
                            "competitor_handle": "ig_coach",
                            "competitor_url": "https://www.instagram.com/ig_coach/",
                            "appearance_count": 2,
                        }
                    ]
                },
            },
        },
        text="Обычный отчет",
        section_counts={},
    )
    monkeypatch.setattr(report_pipeline, "build_report_preview", lambda **kwargs: preview)
    sent: list[dict] = []
    monkeypatch.setattr(report_pipeline, "send_message", lambda **kwargs: sent.append(kwargs) or {"message_id": len(sent)})

    result = create_and_send_report(
        user=user,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    result.report.refresh_from_db()
    assert len(sent) == 2
    assert sent[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"sugigadd:{result.report.id}:0"
    assert result.telegram_result["instagram_suggestion_message_id"] == 2
    assert "suggestion_message_id" not in result.telegram_result
    assert result.report.payload["suggested_competitors"]["instagram"]["diagnostics"]["suggestions_sent"] == 1
    assert (
        result.report.payload["suggested_competitors"]["instagram"]["delivery"]["sent_items"][0]["competitor_external_id"]
        == "ig-1"
    )


def test_create_and_send_report_main_text_unchanged_with_instagram_suggestion_followup(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=1143, tg_chat_id=1143, timezone_str="UTC")
    preview = ReportPreview(
        payload={
            "sections": [],
            "suggested_competitors": {
                "youtube": {"items": []},
                "instagram": {
                    "items": [
                        {
                            "competitor_external_id": "ig-2",
                            "competitor_display_name": "IG Compact",
                            "competitor_handle": "ig_compact",
                            "appearance_count": 2,
                        }
                    ]
                },
            },
        },
        text="Обычный отчет без блока подсказок",
        section_counts={},
    )
    monkeypatch.setattr(report_pipeline, "build_report_preview", lambda **kwargs: preview)
    sent: list[dict] = []
    monkeypatch.setattr(report_pipeline, "send_message", lambda **kwargs: sent.append(kwargs) or {"message_id": len(sent)})

    create_and_send_report(
        user=user,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    assert sent[0]["text"] == "Обычный отчет без блока подсказок"
    assert "Instagram" in sent[1]["text"]
    assert "Обычный отчет без блока подсказок" not in sent[1]["text"]


def test_create_and_send_report_never_sends_blocked_youtube_suggestion(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=1141, tg_chat_id=1141, timezone_str="UTC")
    blocked = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="chan-blocked",
        handle="chan_blocked",
        url="https://www.youtube.com/channel/chan-blocked",
        display_name="Blocked Coach",
    )
    UserCompetitor.objects.create(user=user, competitor=blocked, is_active=False)
    preview = ReportPreview(
        payload={
            "sections": [],
            "suggested_competitors": {
                "youtube": {
                    "items": [
                        {
                            "channel_id": "chan-blocked",
                            "channel_title": "Blocked Coach",
                            "appearance_count": 3,
                        },
                        {
                            "channel_id": "chan-allowed",
                            "channel_title": "Allowed Coach",
                            "appearance_count": 2,
                        },
                    ]
                }
            },
        },
        text="Обычный отчет",
        section_counts={},
    )
    monkeypatch.setattr(report_pipeline, "build_report_preview", lambda **kwargs: preview)
    sent: list[dict] = []
    monkeypatch.setattr(report_pipeline, "send_message", lambda **kwargs: sent.append(kwargs) or {"message_id": len(sent)})

    result = create_and_send_report(
        user=user,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    result.report.refresh_from_db()
    youtube_payload = result.report.payload["suggested_competitors"]["youtube"]
    assert len(sent) == 2
    assert "Allowed Coach" in sent[1]["text"]
    assert "Blocked Coach" not in sent[1]["text"]
    assert youtube_payload["delivery"]["sent_items"][0]["channel_id"] == "chan-allowed"
    assert youtube_payload["delivery"]["suppressed_items"][0]["channel_id"] == "chan-blocked"
    assert youtube_payload["delivery"]["suppressed_items"][0]["suppression_reason"] == "blocked"


def test_create_and_send_report_caps_youtube_suggestion_follow_up_to_one_item_per_run(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=1131, tg_chat_id=1131, timezone_str="UTC")
    preview = ReportPreview(
        payload={
            "sections": [],
            "suggested_competitors": {
                "youtube": {
                    "items": [
                        {"channel_id": "chan-1", "channel_title": "Topic Coach 1", "appearance_count": 3},
                        {"channel_id": "chan-2", "channel_title": "Topic Coach 2", "appearance_count": 3},
                        {"channel_id": "chan-3", "channel_title": "Topic Coach 3", "appearance_count": 2},
                    ]
                }
            },
        },
        text="Обычный отчет",
        section_counts={},
    )
    monkeypatch.setattr(report_pipeline, "build_report_preview", lambda **kwargs: preview)
    sent: list[dict] = []

    def fake_send_message(**kwargs):
        sent.append(kwargs)
        return {"message_id": len(sent)}

    monkeypatch.setattr(report_pipeline, "send_message", fake_send_message)

    result = create_and_send_report(
        user=user,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    result.report.refresh_from_db()
    youtube_payload = result.report.payload["suggested_competitors"]["youtube"]
    assert len(sent) == 2
    assert "Topic Coach 1" in sent[1]["text"]
    assert "Topic Coach 2" not in sent[1]["text"]
    assert youtube_payload["diagnostics"]["suggestions_sent"] == 1
    assert youtube_payload["diagnostics"]["suppressed_by_run_cap"] == 2
    assert youtube_payload["diagnostics"]["suppressed_by_cooldown"] == 0
    assert youtube_payload["delivery"]["sent_items"][0]["channel_id"] == "chan-1"
    assert [item["channel_id"] for item in youtube_payload["delivery"]["suppressed_items"]] == ["chan-2", "chan-3"]
    assert all(item["suppression_reason"] == "run_cap" for item in youtube_payload["delivery"]["suppressed_items"])


@override_settings(REPORT_YOUTUBE_SUGGESTED_COMPETITORS_RESEND_COOLDOWN_HOURS=72)
def test_create_and_send_report_suppresses_recently_sent_youtube_suggestion_by_cooldown(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=1132, tg_chat_id=1132, timezone_str="UTC")
    Report.objects.create(
        user=user,
        period_start=datetime(2026, 3, 21, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 22, 0, 0, tzinfo=UTC),
        status="sent",
        sent_at=datetime(2026, 3, 23, 23, 0, tzinfo=UTC),
        payload={
            "sections": [],
            "suggested_competitors": {
                "youtube": {
                    "items": [{"channel_id": "chan-1", "channel_title": "Recent Coach"}],
                    "delivery": {
                        "message_cap": 1,
                        "cooldown_hours": 72,
                        "sent_items": [
                            {
                                "channel_id": "chan-1",
                                "channel_title": "Recent Coach",
                                "sent_at": "2026-03-23T23:00:00+00:00",
                            }
                        ],
                        "suppressed_items": [],
                    },
                }
            },
        },
    )
    preview = ReportPreview(
        payload={
            "sections": [],
            "suggested_competitors": {
                "youtube": {
                    "items": [
                        {"channel_id": "chan-1", "channel_title": "Recent Coach", "appearance_count": 3},
                        {"channel_id": "chan-2", "channel_title": "Fresh Coach", "appearance_count": 2},
                    ]
                }
            },
        },
        text="Обычный отчет",
        section_counts={},
    )
    monkeypatch.setattr(report_pipeline, "build_report_preview", lambda **kwargs: preview)
    monkeypatch.setattr(report_pipeline.timezone, "now", lambda: datetime(2026, 3, 24, 0, 0, tzinfo=UTC))
    sent: list[dict] = []
    monkeypatch.setattr(report_pipeline, "send_message", lambda **kwargs: sent.append(kwargs) or {"message_id": len(sent)})

    result = create_and_send_report(
        user=user,
        period_start=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 25, 0, 0, tzinfo=UTC),
    )

    result.report.refresh_from_db()
    youtube_payload = result.report.payload["suggested_competitors"]["youtube"]
    assert len(sent) == 2
    assert "Fresh Coach" in sent[1]["text"]
    assert "Recent Coach" not in sent[1]["text"]
    assert youtube_payload["diagnostics"]["suggestions_sent"] == 1
    assert youtube_payload["diagnostics"]["suppressed_by_cooldown"] == 1
    assert youtube_payload["diagnostics"]["suppressed_by_run_cap"] == 0
    assert youtube_payload["delivery"]["sent_items"][0]["channel_id"] == "chan-2"
    assert youtube_payload["delivery"]["suppressed_items"][0]["channel_id"] == "chan-1"
    assert youtube_payload["delivery"]["suppressed_items"][0]["suppression_reason"] == "cooldown"


@override_settings(REPORT_YOUTUBE_SUGGESTED_COMPETITORS_RESEND_COOLDOWN_HOURS=72)
def test_create_and_send_report_allows_youtube_suggestion_after_cooldown_expires(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=1133, tg_chat_id=1133, timezone_str="UTC")
    Report.objects.create(
        user=user,
        period_start=datetime(2026, 3, 18, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 19, 0, 0, tzinfo=UTC),
        status="sent",
        sent_at=datetime(2026, 3, 20, 0, 0, tzinfo=UTC),
        payload={
            "sections": [],
            "suggested_competitors": {
                "youtube": {
                    "items": [{"channel_id": "chan-1", "channel_title": "Repeat Coach"}],
                    "delivery": {
                        "message_cap": 1,
                        "cooldown_hours": 72,
                        "sent_items": [
                            {
                                "channel_id": "chan-1",
                                "channel_title": "Repeat Coach",
                                "sent_at": "2026-03-20T00:00:00+00:00",
                            }
                        ],
                        "suppressed_items": [],
                    },
                }
            },
        },
    )
    preview = ReportPreview(
        payload={
            "sections": [],
            "suggested_competitors": {
                "youtube": {
                    "items": [{"channel_id": "chan-1", "channel_title": "Repeat Coach", "appearance_count": 4}]
                }
            },
        },
        text="Обычный отчет",
        section_counts={},
    )
    monkeypatch.setattr(report_pipeline, "build_report_preview", lambda **kwargs: preview)
    monkeypatch.setattr(report_pipeline.timezone, "now", lambda: datetime(2026, 3, 24, 12, 0, tzinfo=UTC))
    sent: list[dict] = []
    monkeypatch.setattr(report_pipeline, "send_message", lambda **kwargs: sent.append(kwargs) or {"message_id": len(sent)})

    result = create_and_send_report(
        user=user,
        period_start=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 25, 0, 0, tzinfo=UTC),
    )

    result.report.refresh_from_db()
    youtube_payload = result.report.payload["suggested_competitors"]["youtube"]
    assert len(sent) == 2
    assert "Repeat Coach" in sent[1]["text"]
    assert youtube_payload["diagnostics"]["suggestions_sent"] == 1
    assert youtube_payload["diagnostics"]["suppressed_by_cooldown"] == 0
    assert youtube_payload["diagnostics"]["suppressed_by_run_cap"] == 0
    assert youtube_payload["delivery"]["sent_items"][0]["channel_id"] == "chan-1"


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

    monkeypatch.setattr(report_pipeline, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (True, 2))
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
    monkeypatch.setattr(report_pipeline, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (True, 2))
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


def test_build_report_preview_skips_youtube_competitors_without_recent_shorts(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=142, tg_chat_id=142, timezone_str="UTC")
    youtube = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="yt-no-shorts",
        handle="yt_no_shorts",
        url="https://youtube.com/@yt_no_shorts",
        display_name="YT No Shorts",
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
    calls = {"youtube_refresh": 0, "instagram_refresh": 0}

    def fake_refresh(*, competitor, mode, captured_at, provider_fetch_cache=None):
        if competitor.platform == Platform.YOUTUBE:
            calls["youtube_refresh"] += 1
            raise AssertionError("YouTube competitor without recent shorts should be skipped before refresh")
        calls["instagram_refresh"] += 1
        item = competitor.content_items.create(
            platform=competitor.platform,
            external_id=f"item-{competitor.external_id}",
            url=competitor.url,
            title="Latest reel",
            description="desc",
            published_at=now - timedelta(hours=2),
            duration_seconds=30,
            meta={"content_type": "reel"},
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

    monkeypatch.setattr(report_pipeline, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (False, 0))
    monkeypatch.setattr(report_pipeline, "refresh_competitor", fake_refresh)

    preview = build_report_preview(
        user=user,
        period_start=now - timedelta(hours=24),
        period_end=now,
    )

    sections = {section["platform"]: section for section in preview.payload["sections"]}
    assert sections[Platform.YOUTUBE]["items"] == []
    assert sections[Platform.INSTAGRAM]["items"] != []
    assert calls == {"youtube_refresh": 0, "instagram_refresh": 1}


def test_build_report_preview_reports_no_active_competitors_reason(db):
    user = TgUser.objects.create(tg_user_id=143, tg_chat_id=143, timezone_str="UTC")

    preview = build_report_preview(
        user=user,
        period_start=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 12, 0, tzinfo=UTC),
    )

    sections = {section["platform"]: section for section in preview.payload["sections"]}
    youtube_diag = sections[Platform.YOUTUBE]["diagnostics"]
    assert youtube_diag["active_competitors"] == 0
    assert youtube_diag["final_items"] == 0
    assert youtube_diag["empty_reason"] == "no_active_competitors"
    assert "Причина: Нет активных конкурентов для этой платформы." in preview.text


@override_settings(MAX_COMPETITORS_PER_PLATFORM=1, ENABLE_YOUTUBE_SUPPLEMENTAL_TOPIC_VIDEO_COLLECTION=False)
def test_build_report_preview_uses_all_active_competitors_without_truncation(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=171, tg_chat_id=171, timezone_str="UTC")
    competitors = [
        Competitor.objects.create(
            platform=Platform.YOUTUBE,
            external_id=f"yt-{idx}",
            handle=f"yt_{idx}",
            url=f"https://www.youtube.com/@yt_{idx}",
            display_name=f"YT {idx}",
        )
        for idx in range(21)
    ]
    for competitor in competitors:
        UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)

    seen_refreshes: list[str] = []

    monkeypatch.setattr(report_pipeline, "_prefetch_provider_data_for_competitors", lambda **kwargs: None)
    monkeypatch.setattr(report_pipeline, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (True, 2))
    monkeypatch.setattr(
        report_pipeline,
        "refresh_competitor",
        lambda **kwargs: (seen_refreshes.append(kwargs["competitor"].external_id) or []),
    )
    monkeypatch.setattr(report_pipeline, "compute_competitor_baseline", lambda **kwargs: None)
    monkeypatch.setattr(report_pipeline, "score_items_for_period", lambda **kwargs: [])

    preview = build_report_preview(
        user=user,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    youtube_diag = next(section["diagnostics"] for section in preview.payload["sections"] if section["platform"] == Platform.YOUTUBE)
    assert len(seen_refreshes) == 21
    assert youtube_diag["active_competitors"] == 21


def test_build_report_preview_includes_youtube_collection_depth_diagnostics(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=1431, tg_chat_id=1431, timezone_str="UTC")
    competitor = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="yt-depth",
        handle="yt_depth",
        url="https://youtube.com/@yt_depth",
        display_name="YT Depth",
    )
    UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)
    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)

    def fake_refresh(*, competitor, mode, captured_at, provider_fetch_cache=None):
        provider_fetch_cache.store_youtube_refresh_diagnostics(
            competitor_id=competitor.id,
            diagnostics={
                "uploads_pages_scanned": 2,
                "uploads_inspected": 75,
                "short_form_items_found": 4,
                "usable_short_form_items_returned": 3,
                "deeper_pages_used": True,
            },
        )
        item = competitor.content_items.create(
            platform=competitor.platform,
            external_id="yt-depth-1",
            url="https://www.youtube.com/watch?v=yt-depth-1",
            title="Depth short",
            description="desc",
            published_at=now - timedelta(hours=2),
            duration_seconds=30,
            meta={"content_type": "short"},
        )
        MetricSnapshot.objects.create(
            content_item=item,
            captured_at=now,
            views=2000,
            likes=100,
            comments=10,
            shares=1,
        )
        return [item]

    monkeypatch.setattr(report_pipeline, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (True, 2))
    monkeypatch.setattr(report_pipeline, "refresh_competitor", fake_refresh)

    preview = build_report_preview(
        user=user,
        period_start=now - timedelta(hours=24),
        period_end=now,
    )

    sections = {section["platform"]: section for section in preview.payload["sections"]}
    youtube_diag = sections[Platform.YOUTUBE]["diagnostics"]
    assert youtube_diag["youtube_uploads_pages_scanned"] == 2
    assert youtube_diag["youtube_uploads_inspected"] == 75
    assert youtube_diag["youtube_short_form_items_found"] == 4
    assert youtube_diag["youtube_usable_short_form_items_returned"] == 3


def test_build_report_preview_reports_scoring_filtered_reason_and_counters(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=144, tg_chat_id=144, timezone_str="UTC")
    competitor = Competitor.objects.create(
        platform=Platform.INSTAGRAM,
        external_id="ig-old",
        handle="old_reels",
        url="https://www.instagram.com/old_reels/",
        display_name="Old Reels",
    )
    UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)
    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)

    def fake_refresh(*, competitor, mode, captured_at, provider_fetch_cache=None):
        item = competitor.content_items.create(
            platform=competitor.platform,
            external_id="old-reel-1",
            url="https://www.instagram.com/reel/old-reel-1/",
            title="Old reel",
            description="desc",
            published_at=now - timedelta(days=61),
            duration_seconds=30,
            meta={"content_type": "reel"},
        )
        MetricSnapshot.objects.create(
            content_item=item,
            captured_at=now,
            views=3000,
            likes=100,
            comments=10,
            shares=1,
        )
        return [item]

    monkeypatch.setattr(report_pipeline, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (True, 2))
    monkeypatch.setattr(report_pipeline, "refresh_competitor", fake_refresh)

    preview = build_report_preview(
        user=user,
        period_start=now - timedelta(hours=24),
        period_end=now,
    )

    sections = {section["platform"]: section for section in preview.payload["sections"]}
    instagram_diag = sections[Platform.INSTAGRAM]["diagnostics"]
    assert instagram_diag["active_competitors"] == 1
    assert instagram_diag["refreshed_items"] == 1
    assert instagram_diag["dropped_by_age"] == 1
    assert instagram_diag["final_items"] == 0
    assert instagram_diag["empty_reason"] == "all_filtered_by_scoring"
    assert "Причина: Ролики были, но текущие фильтры отбора ничего не пропустили." in preview.text


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


def test_build_report_preview_reports_stopwords_reason_and_payload_diagnostics(db, monkeypatch):
    user = TgUser.objects.create(tg_user_id=161, tg_chat_id=161, timezone_str="UTC", report_stopwords=["spoiler"])
    competitor = Competitor.objects.create(
        platform=Platform.INSTAGRAM,
        external_id="ig-stopword",
        handle="creator",
        url="https://www.instagram.com/creator/",
        display_name="Creator",
    )
    UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)
    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)

    def fake_refresh(*, competitor, mode, captured_at, provider_fetch_cache=None):
        item = competitor.content_items.create(
            platform=competitor.platform,
            external_id="ig-stopword-1",
            url="https://www.instagram.com/reel/ig-stopword-1/",
            title="Major spoiler reel",
            description="desc",
            published_at=now - timedelta(hours=2),
            duration_seconds=30,
            meta={"content_type": "reel"},
        )
        MetricSnapshot.objects.create(
            content_item=item,
            captured_at=now,
            views=5000,
            likes=100,
            comments=10,
            shares=1,
        )
        return [item]

    monkeypatch.setattr(report_pipeline, "refresh_competitor", fake_refresh)

    preview = build_report_preview(
        user=user,
        period_start=now - timedelta(hours=24),
        period_end=now,
    )

    sections = {section["platform"]: section for section in preview.payload["sections"]}
    instagram_diag = sections[Platform.INSTAGRAM]["diagnostics"]
    assert instagram_diag["active_competitors"] == 1
    assert instagram_diag["refreshed_items"] == 1
    assert instagram_diag["scored_items"] == 1
    assert instagram_diag["dropped_by_stopwords"] == 1
    assert instagram_diag["final_items"] == 0
    assert instagram_diag["empty_reason"] == "all_filtered_by_stopwords"
    assert "Причина: Все подходящие ролики скрыты вашими стоп-словами." in preview.text


def test_build_report_preview_keeps_strict_items_when_platform_already_has_enough(db, monkeypatch, settings):
    settings.REPORT_FALLBACK_MIN_ITEMS_PER_PLATFORM = 2
    user = TgUser.objects.create(tg_user_id=171, tg_chat_id=171, timezone_str="UTC")
    competitor = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="yt-strict-enough",
        handle="yt_strict_enough",
        url="https://youtube.com/@yt_strict_enough",
        display_name="YT Strict Enough",
    )
    UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)
    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)
    first = competitor.content_items.create(
        platform=competitor.platform,
        external_id="strict-1",
        url="https://www.youtube.com/watch?v=strict-1",
        title="Strict 1",
        description="desc",
        published_at=now - timedelta(hours=2),
        duration_seconds=30,
        meta={"content_type": "short"},
    )
    second = competitor.content_items.create(
        platform=competitor.platform,
        external_id="strict-2",
        url="https://www.youtube.com/watch?v=strict-2",
        title="Strict 2",
        description="desc",
        published_at=now - timedelta(hours=1),
        duration_seconds=30,
        meta={"content_type": "short"},
    )
    fallback = competitor.content_items.create(
        platform=competitor.platform,
        external_id="fallback-1",
        url="https://www.youtube.com/watch?v=fallback-1",
        title="Fallback 1",
        description="desc",
        published_at=now - timedelta(hours=3),
        duration_seconds=30,
        meta={"content_type": "short"},
    )

    monkeypatch.setattr(report_pipeline, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (True, 2))
    monkeypatch.setattr(report_pipeline, "refresh_competitor", lambda **kwargs: [first, second, fallback])
    monkeypatch.setattr(report_pipeline, "compute_competitor_baseline", lambda **kwargs: SimpleNamespace(vph_median=1.0, rph_median=1.0))

    def fake_score_items_for_period(**kwargs):
        selection_mode = kwargs["selection_mode"]
        if selection_mode == "strict":
            return [
                SimpleNamespace(content_item=first, competitor=competitor, views_end=5000, likes_end=100, comments_end=10, shares_end=1, velocity=250.0, score_type="delta", delta_views=1000, delta_hours=4.0, er_end=0.022, base_score=3.0, adaptation_relevance_score=0.4, adaptation_relevance_factors={"niche_stem_overlap": 0.22}, selection_path="strict", fallback_reason=None, score=3.4),
                SimpleNamespace(content_item=second, competitor=competitor, views_end=4500, likes_end=90, comments_end=9, shares_end=1, velocity=220.0, score_type="delta", delta_views=900, delta_hours=4.0, er_end=0.021, base_score=2.8, adaptation_relevance_score=0.3, adaptation_relevance_factors={"niche_stem_overlap": 0.22}, selection_path="strict", fallback_reason=None, score=3.1),
            ]
        return [
            SimpleNamespace(content_item=fallback, competitor=competitor, views_end=3000, likes_end=70, comments_end=7, shares_end=1, velocity=150.0, score_type="fallback_delta", delta_views=180, delta_hours=4.0, er_end=0.019, base_score=2.0, adaptation_relevance_score=0.45, adaptation_relevance_factors={"niche_stem_overlap": 0.22}, selection_path="fallback", fallback_reason="delta_threshold_relaxed", score=2.45),
        ]

    monkeypatch.setattr(report_pipeline, "score_items_for_period", fake_score_items_for_period)

    preview = build_report_preview(
        user=user,
        period_start=now - timedelta(hours=24),
        period_end=now,
    )

    section = next(section for section in preview.payload["sections"] if section["platform"] == Platform.YOUTUBE)
    assert [item["video_id"] for item in section["items"]] == ["strict-1", "strict-2"]
    assert section["diagnostics"]["strict_items"] == 2
    assert section["diagnostics"]["fallback_items"] == 0


def test_build_report_preview_adds_fallback_items_when_strict_path_is_thin(db, monkeypatch, settings):
    settings.REPORT_FALLBACK_MIN_ITEMS_PER_PLATFORM = 2
    settings.REPORT_FALLBACK_MAX_ITEMS_PER_PLATFORM = 2
    user = TgUser.objects.create(tg_user_id=172, tg_chat_id=172, timezone_str="UTC")
    competitor = Competitor.objects.create(
        platform=Platform.INSTAGRAM,
        external_id="ig-fallback-thin",
        handle="ig_fallback_thin",
        url="https://www.instagram.com/ig_fallback_thin/",
        display_name="IG Fallback Thin",
    )
    UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)
    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)
    strict_item = competitor.content_items.create(
        platform=competitor.platform,
        external_id="strict-item",
        url="https://www.instagram.com/reel/strict-item/",
        title="Strict item",
        description="desc",
        published_at=now - timedelta(hours=2),
        duration_seconds=30,
        meta={"content_type": "reel"},
    )
    fallback_item = competitor.content_items.create(
        platform=competitor.platform,
        external_id="fallback-item",
        url="https://www.instagram.com/reel/fallback-item/",
        title="Fallback item",
        description="desc",
        published_at=now - timedelta(hours=1),
        duration_seconds=30,
        meta={"content_type": "reel"},
    )

    monkeypatch.setattr(report_pipeline, "refresh_competitor", lambda **kwargs: [strict_item, fallback_item])
    monkeypatch.setattr(report_pipeline, "compute_competitor_baseline", lambda **kwargs: SimpleNamespace(vph_median=1.0, rph_median=1.0))

    def fake_score_items_for_period(**kwargs):
        selection_mode = kwargs["selection_mode"]
        if selection_mode == "strict":
            return [
                SimpleNamespace(content_item=strict_item, competitor=competitor, views_end=5000, likes_end=100, comments_end=10, shares_end=1, velocity=250.0, score_type="delta", delta_views=1000, delta_hours=4.0, er_end=0.022, base_score=3.0, adaptation_relevance_score=0.4, adaptation_relevance_factors={"niche_stem_overlap": 0.22}, selection_path="strict", fallback_reason=None, score=3.4),
            ]
        return [
            SimpleNamespace(content_item=fallback_item, competitor=competitor, views_end=3200, likes_end=80, comments_end=8, shares_end=1, velocity=160.0, score_type="fallback_delta", delta_views=180, delta_hours=4.0, er_end=0.02, base_score=2.1, adaptation_relevance_score=0.5, adaptation_relevance_factors={"instructional_markers": 0.2}, selection_path="fallback", fallback_reason="delta_threshold_relaxed", score=2.6),
        ]

    monkeypatch.setattr(report_pipeline, "score_items_for_period", fake_score_items_for_period)

    preview = build_report_preview(
        user=user,
        period_start=now - timedelta(hours=24),
        period_end=now,
    )

    section = next(section for section in preview.payload["sections"] if section["platform"] == Platform.INSTAGRAM)
    assert [item["video_id"] for item in section["items"]] == ["strict-item", "fallback-item"]
    assert section["items"][1]["selection_path"] == "fallback"
    assert section["items"][1]["fallback_reason"] == "delta_threshold_relaxed"
    assert section["diagnostics"]["strict_items"] == 1
    assert section["diagnostics"]["fallback_candidates"] == 1
    assert section["diagnostics"]["fallback_items"] == 1
    assert section["diagnostics"]["fallback_reasons_used"] == {"delta_threshold_relaxed": 1}


def test_build_report_preview_keeps_main_sections_unchanged_when_supplemental_collection_enabled(db, monkeypatch, settings):
    settings.ENABLE_YOUTUBE_SUPPLEMENTAL_TOPIC_VIDEO_COLLECTION = True
    user = TgUser.objects.create(tg_user_id=181, tg_chat_id=181, timezone_str="UTC")
    competitor = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="yt-main-with-supplemental",
        handle="yt_main_with_supplemental",
        url="https://www.youtube.com/@yt_main_with_supplemental",
        display_name="YT Main With Supplemental",
        meta={"uploads_playlist_id": "UU-main-with-supplemental"},
    )
    UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)
    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)
    item = competitor.content_items.create(
        platform=competitor.platform,
        external_id="strict-main-item",
        url="https://www.youtube.com/watch?v=strict-main-item",
        title="Strict main item",
        description="desc",
        published_at=now - timedelta(hours=2),
        duration_seconds=30,
        meta={"content_type": "short"},
    )

    monkeypatch.setattr(report_pipeline, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (True, 2))
    monkeypatch.setattr(report_pipeline, "refresh_competitor", lambda **kwargs: [item])
    monkeypatch.setattr(report_pipeline, "compute_competitor_baseline", lambda **kwargs: SimpleNamespace(vph_median=1.0, rph_median=1.0))
    monkeypatch.setattr(report_pipeline, "_classify_item_drop_reason", lambda **kwargs: None)
    monkeypatch.setattr(
        report_pipeline,
        "score_items_for_period",
        lambda **kwargs: [
            SimpleNamespace(
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
                base_score=3.0,
                adaptation_relevance_score=0.4,
                adaptation_relevance_factors={"niche_stem_overlap": 0.22},
                selection_path="strict",
                fallback_reason=None,
                score=3.4,
            )
        ],
    )
    monkeypatch.setattr(
        report_pipeline,
        "collect_youtube_topic_video_candidates",
        lambda **kwargs: SimpleNamespace(
            to_payload=lambda: {
                "source": "supplemental_topic_video",
                "queries": ["spoken english"],
                "diagnostics": {"queries_built": 1, "queries_executed": 1, "final_candidates": 1},
                "candidates": [
                    {
                        "source": "supplemental_topic_video",
                        "video_id": "supp-1",
                        "matched_queries": ["spoken english"],
                        "hit_count": 1,
                        "first_seen_rank": 1,
                    }
                ],
            },
            diagnostics={"queries_built": 1, "queries_executed": 1, "final_candidates": 1},
        ),
    )

    preview = build_report_preview(
        user=user,
        period_start=now - timedelta(hours=24),
        period_end=now,
    )

    youtube_section = next(section for section in preview.payload["sections"] if section["platform"] == Platform.YOUTUBE)
    assert [item["video_id"] for item in youtube_section["items"]] == ["strict-main-item"]
    assert preview.section_counts[Platform.YOUTUBE] == 1
    assert preview.payload["supplemental"]["youtube_topic_video"]["diagnostics"]["final_candidates"] == 1
    assert preview.payload["supplemental"]["youtube_topic_video"]["candidates"][0]["video_id"] == "supp-1"


def test_build_report_preview_renders_youtube_supplemental_section_without_main_duplicates(db, monkeypatch, settings):
    settings.ENABLE_YOUTUBE_SUPPLEMENTAL_TOPIC_VIDEO_COLLECTION = True
    settings.REPORT_YOUTUBE_SUPPLEMENTAL_MAX_ITEMS = 10
    user = TgUser.objects.create(tg_user_id=182, tg_chat_id=182, timezone_str="UTC")
    competitor = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="yt-main-supp-render",
        handle="yt_main_supp_render",
        url="https://www.youtube.com/@yt_main_supp_render",
        display_name="YT Main Supp Render",
        meta={"uploads_playlist_id": "UU-main-supp-render"},
    )
    UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)
    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)
    item = competitor.content_items.create(
        platform=competitor.platform,
        external_id="strict-main-item",
        url="https://www.youtube.com/watch?v=strict-main-item",
        title="Strict main item",
        description="desc",
        published_at=now - timedelta(hours=2),
        duration_seconds=30,
        meta={"content_type": "short"},
    )

    monkeypatch.setattr(report_pipeline, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (True, 2))
    monkeypatch.setattr(report_pipeline, "refresh_competitor", lambda **kwargs: [item])
    monkeypatch.setattr(report_pipeline, "compute_competitor_baseline", lambda **kwargs: SimpleNamespace(vph_median=1.0, rph_median=1.0))
    monkeypatch.setattr(report_pipeline, "_classify_item_drop_reason", lambda **kwargs: None)
    monkeypatch.setattr(
        report_pipeline,
        "score_items_for_period",
        lambda **kwargs: [
            SimpleNamespace(
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
                base_score=3.0,
                adaptation_relevance_score=0.4,
                adaptation_relevance_factors={"niche_stem_overlap": 0.22},
                selection_path="strict",
                fallback_reason=None,
                score=3.4,
            )
        ],
    )
    monkeypatch.setattr(
        report_pipeline,
        "collect_youtube_topic_video_candidates",
        lambda **kwargs: SimpleNamespace(
            to_payload=lambda: {
                "source": "supplemental_topic_video",
                "queries": ["spoken english"],
                "diagnostics": {"queries_built": 1, "queries_executed": 1, "final_candidates": 3},
                "candidates": [
                    {
                        "source": "supplemental_topic_video",
                        "video_id": "strict-main-item",
                        "url": "https://www.youtube.com/watch?v=strict-main-item",
                        "title": "Main duplicate",
                        "channel_title": "Duplicate Channel",
                        "views": 7000,
                        "matched_queries": ["spoken english"],
                        "hit_count": 1,
                        "first_seen_rank": 1,
                    },
                    {
                        "source": "supplemental_topic_video",
                        "video_id": "supp-1",
                        "url": "https://www.youtube.com/watch?v=supp-1",
                        "title": "Supplemental idea one",
                        "channel_title": "Topic Coach",
                        "views": 6100,
                        "matched_queries": ["spoken english"],
                        "hit_count": 1,
                        "first_seen_rank": 2,
                    },
                    {
                        "source": "supplemental_topic_video",
                        "video_id": "supp-2",
                        "url": "https://www.youtube.com/watch?v=supp-2",
                        "title": "Supplemental idea two",
                        "channel_title": "Format Coach",
                        "views": 5900,
                        "matched_queries": ["spoken english"],
                        "hit_count": 1,
                        "first_seen_rank": 3,
                    },
                ],
            },
            diagnostics={"queries_built": 1, "queries_executed": 1, "final_candidates": 3},
        ),
    )

    preview = build_report_preview(
        user=user,
        period_start=now - timedelta(hours=24),
        period_end=now,
    )

    youtube_supplemental = preview.payload["supplemental"]["youtube_topic_video"]
    assert [item["video_id"] for item in youtube_supplemental["section"]["items"]] == ["supp-1", "supp-2"]
    assert youtube_supplemental["diagnostics"]["dropped_by_main_report_duplicate"] == 1
    assert "Дополнительно в YouTube:" in preview.text
    assert "Supplemental idea one" in preview.text
    assert "Main duplicate" not in preview.text


def test_build_report_preview_attaches_youtube_suggested_competitors_payload(db, monkeypatch, settings):
    settings.ENABLE_YOUTUBE_SUPPLEMENTAL_TOPIC_VIDEO_COLLECTION = True
    user = TgUser.objects.create(tg_user_id=183, tg_chat_id=183, timezone_str="UTC")
    competitor = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="yt-suggested-payload",
        handle="yt_suggested_payload",
        url="https://www.youtube.com/@yt_suggested_payload",
        display_name="YT Suggested Payload",
        meta={"uploads_playlist_id": "UU-suggested-payload"},
    )
    UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)
    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)
    Report.objects.create(
        user=user,
        period_start=now - timedelta(days=2),
        period_end=now - timedelta(days=1),
        status="sent",
        payload={
            "sections": [],
            "supplemental": {
                "youtube_topic_video": {
                    "source": "supplemental_topic_video",
                    "candidates": [
                        {
                            "video_id": "hist-sugg",
                            "url": "https://www.youtube.com/watch?v=hist-sugg",
                            "title": "Historic suggested idea",
                            "channel_id": "chan-repeat",
                            "channel_title": "Topic Coach",
                            "matched_queries": ["spoken english"],
                            "views": 5000,
                            "likes": 180,
                            "comments": 20,
                            "supplemental_score": 0.61,
                            "supplemental_survival_reason": "deterministic_rank_pass",
                            "supplemental_ranking_factors": {
                                "instructional_markers": 0.18,
                                "query_phrase_match": 0.18,
                                "traction": 0.14,
                            },
                        }
                    ],
                }
            },
        },
    )

    monkeypatch.setattr(report_pipeline, "youtube_profile_recent_shorts_gate_status", lambda **kwargs: (True, 2))
    monkeypatch.setattr(report_pipeline, "refresh_competitor", lambda **kwargs: [])
    monkeypatch.setattr(report_pipeline, "compute_competitor_baseline", lambda **kwargs: None)
    monkeypatch.setattr(report_pipeline, "score_items_for_period", lambda **kwargs: [])
    monkeypatch.setattr(
        report_pipeline,
        "collect_youtube_topic_video_candidates",
        lambda **kwargs: SimpleNamespace(
            to_payload=lambda: {
                "source": "supplemental_topic_video",
                "queries": ["spoken english"],
                "diagnostics": {"final_candidates": 1},
                "candidates": [
                    {
                        "video_id": "curr-sugg",
                        "url": "https://www.youtube.com/watch?v=curr-sugg",
                        "title": "Current suggested idea",
                        "channel_id": "chan-repeat",
                        "channel_title": "Topic Coach",
                        "matched_queries": ["spoken english"],
                        "views": 5400,
                        "likes": 190,
                        "comments": 24,
                        "supplemental_score": 0.64,
                        "supplemental_survival_reason": "deterministic_rank_pass",
                        "supplemental_ranking_factors": {
                            "instructional_markers": 0.18,
                            "query_phrase_match": 0.18,
                            "traction": 0.14,
                        },
                    }
                ],
            },
            diagnostics={"final_candidates": 1},
        ),
    )

    preview = build_report_preview(
        user=user,
        period_start=now - timedelta(hours=24),
        period_end=now,
    )

    suggestions = preview.payload["suggested_competitors"]["youtube"]
    assert suggestions["diagnostics"]["final_suggestions"] == 1
    assert suggestions["items"][0]["channel_id"] == "chan-repeat"
    assert "instagram" in preview.payload["suggested_competitors"]


def test_build_report_preview_fallback_still_respects_already_reported_and_stopwords(db, monkeypatch, settings):
    settings.REPORT_FALLBACK_MIN_ITEMS_PER_PLATFORM = 2
    user = TgUser.objects.create(tg_user_id=173, tg_chat_id=173, timezone_str="UTC", report_stopwords=["blocked"])
    competitor = Competitor.objects.create(
        platform=Platform.INSTAGRAM,
        external_id="ig-fallback-blocked",
        handle="ig_fallback_blocked",
        url="https://www.instagram.com/ig_fallback_blocked/",
        display_name="IG Fallback Blocked",
    )
    UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)
    now = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)
    already_reported = competitor.content_items.create(
        platform=competitor.platform,
        external_id="already-reported",
        url="https://www.instagram.com/reel/already-reported/",
        title="Relevant fallback one",
        description="desc",
        published_at=now - timedelta(hours=2),
        duration_seconds=30,
        meta={"content_type": "reel"},
    )
    blocked = competitor.content_items.create(
        platform=competitor.platform,
        external_id="blocked-fallback",
        url="https://www.instagram.com/reel/blocked-fallback/",
        title="Blocked fallback",
        description="blocked topic",
        published_at=now - timedelta(hours=1),
        duration_seconds=30,
        meta={"content_type": "reel"},
    )
    Report.objects.create(
        user=user,
        period_start=now - timedelta(days=1),
        period_end=now - timedelta(hours=12),
        status="sent",
        payload={
            "sections": [
                {
                    "platform": Platform.INSTAGRAM,
                    "items": [{"platform": Platform.INSTAGRAM, "video_id": "already-reported", "url": already_reported.url}],
                }
            ]
        },
    )

    monkeypatch.setattr(report_pipeline, "refresh_competitor", lambda **kwargs: [already_reported, blocked])
    monkeypatch.setattr(report_pipeline, "compute_competitor_baseline", lambda **kwargs: SimpleNamespace(vph_median=1.0, rph_median=1.0))

    def fake_score_items_for_period(**kwargs):
        if kwargs["selection_mode"] == "strict":
            return []
        return [
            SimpleNamespace(content_item=already_reported, competitor=competitor, views_end=3200, likes_end=80, comments_end=8, shares_end=1, velocity=160.0, score_type="fallback_delta", delta_views=180, delta_hours=4.0, er_end=0.02, base_score=2.1, adaptation_relevance_score=0.5, adaptation_relevance_factors={"instructional_markers": 0.2}, selection_path="fallback", fallback_reason="delta_threshold_relaxed", score=2.6),
            SimpleNamespace(content_item=blocked, competitor=competitor, views_end=3000, likes_end=75, comments_end=7, shares_end=1, velocity=150.0, score_type="fallback_delta", delta_views=170, delta_hours=4.0, er_end=0.019, base_score=2.0, adaptation_relevance_score=0.45, adaptation_relevance_factors={"instructional_markers": 0.2}, selection_path="fallback", fallback_reason="delta_threshold_relaxed", score=2.45),
        ]

    monkeypatch.setattr(report_pipeline, "score_items_for_period", fake_score_items_for_period)

    preview = build_report_preview(
        user=user,
        period_start=now - timedelta(hours=24),
        period_end=now,
    )

    section = next(section for section in preview.payload["sections"] if section["platform"] == Platform.INSTAGRAM)
    assert section["items"] == []
    assert section["diagnostics"]["fallback_candidates"] == 2
    assert section["diagnostics"]["fallback_items"] == 0
    assert section["diagnostics"]["fallback_rejected_by_already_reported"] == 1
    assert section["diagnostics"]["fallback_rejected_by_stopwords"] == 1


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
