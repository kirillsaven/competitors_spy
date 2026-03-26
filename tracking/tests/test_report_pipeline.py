from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tracking.models import TgUser
from tracking.services import report_pipeline
from tracking.services.collector import CollectorError
from tracking.services.report_pipeline import ReportPipelineError, ReportPreview, assert_required_platform_sections


def test_assert_required_platform_sections_raises_for_empty_requested_platform():
    preview = ReportPreview(
        payload={},
        text="report",
        section_counts={"youtube": 0, "tiktok": 5, "instagram": 0},
        collection_failures=[],
    )

    with pytest.raises(
        ReportPipelineError,
        match="instagram verification produced an empty report section with the current provider data and scoring thresholds",
    ):
        assert_required_platform_sections(preview=preview, required_platforms={"instagram"})


@pytest.mark.django_db
def test_build_report_preview_keeps_partial_failures_in_payload_and_text(monkeypatch):
    user = TgUser.objects.create(tg_user_id=1001, tg_chat_id=1001, timezone_str="UTC")
    good_competitor = SimpleNamespace(
        id=1,
        platform="youtube",
        display_name="Good Channel",
        handle="good-channel",
        external_id="yt-good",
    )
    broken_competitor = SimpleNamespace(
        id=2,
        platform="instagram",
        display_name="Broken Gram",
        handle="broken-gram",
        external_id="ig-broken",
    )
    item = SimpleNamespace(id=11, competitor=good_competitor)

    monkeypatch.setattr(report_pipeline, "get_active_competitors", lambda *, user: [good_competitor, broken_competitor])

    def fake_refresh_competitor(*, competitor, mode, captured_at, provider_fetch_cache=None):
        if competitor.external_id == "ig-broken":
            raise CollectorError("Instagram profile returned no recent items with views: username=broken-gram")
        return [item]

    monkeypatch.setattr(report_pipeline, "refresh_competitor", fake_refresh_competitor)
    monkeypatch.setattr(
        report_pipeline,
        "compute_competitor_baseline",
        lambda *, competitor, now: {"vph_median": 100.0, "rph_median": 5.0, "n": 0},
    )
    monkeypatch.setattr(report_pipeline, "score_items_for_period", lambda **kwargs: [])

    preview = report_pipeline.build_report_preview(
        user=user,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    assert preview.section_counts == {"youtube": 0, "tiktok": 0, "instagram": 0}
    assert preview.payload["collection_failures"] == [
        {
            "platform": "instagram",
            "competitor": {
                "id": 2,
                "display_name": "Broken Gram",
                "handle": "broken-gram",
            },
            "reason": "Instagram profile returned no recent items with views: username=broken-gram",
        }
    ]
    assert "Проблемы при сборе:" in preview.text
    assert "- Broken Gram: Instagram profile returned no recent items with views: username=broken-gram" in preview.text
    assert "Недостаточно истории для обычного аномального отчета" in preview.text


@pytest.mark.django_db
def test_build_report_preview_explains_when_baseline_is_not_ready(monkeypatch):
    user = TgUser.objects.create(tg_user_id=1010, tg_chat_id=1010, timezone_str="UTC")
    competitor = SimpleNamespace(
        id=10,
        platform="youtube",
        display_name="Good Channel",
        handle="good-channel",
        external_id="yt-good",
    )
    item = SimpleNamespace(id=21, competitor=competitor)

    monkeypatch.setattr(report_pipeline, "get_active_competitors", lambda *, user: [competitor])
    monkeypatch.setattr(report_pipeline, "refresh_competitor", lambda **kwargs: [item])
    monkeypatch.setattr(
        report_pipeline,
        "compute_competitor_baseline",
        lambda *, competitor, now: {"vph_median": 100.0, "rph_median": 5.0, "n": 0},
    )
    monkeypatch.setattr(report_pipeline, "score_items_for_period", lambda **kwargs: [])

    preview = report_pipeline.build_report_preview(
        user=user,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    assert preview.payload["report_reason_code"] == "baseline_not_ready"
    assert "первый проверочный сбор уже сохранен" in preview.payload["report_reason"]


@pytest.mark.django_db
def test_build_report_preview_fails_only_when_all_competitors_fail(monkeypatch):
    user = TgUser.objects.create(tg_user_id=1002, tg_chat_id=1002, timezone_str="UTC")
    broken_competitor = SimpleNamespace(
        id=3,
        platform="instagram",
        display_name="Broken Gram",
        handle="broken-gram",
        external_id="ig-broken",
    )

    monkeypatch.setattr(report_pipeline, "get_active_competitors", lambda *, user: [broken_competitor])
    monkeypatch.setattr(
        report_pipeline,
        "refresh_competitor",
        lambda **kwargs: (_ for _ in ()).throw(
            CollectorError("Instagram profile returned no recent items with views: username=broken-gram")
        ),
    )

    with pytest.raises(ReportPipelineError, match="all competitor refreshes failed"):
        report_pipeline.build_report_preview(
            user=user,
            period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
            period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
        )


@pytest.mark.django_db
def test_build_setup_verification_preview_summarizes_platform_counts(monkeypatch):
    user = TgUser.objects.create(tg_user_id=1003, tg_chat_id=1003, timezone_str="UTC")
    youtube_competitor = SimpleNamespace(
        id=4,
        platform="youtube",
        display_name="Teacher Hub",
        handle="teacher-hub",
        external_id="yt-4",
    )
    instagram_competitor = SimpleNamespace(
        id=5,
        platform="instagram",
        display_name="Broken Gram",
        handle="broken-gram",
        external_id="ig-5",
    )
    item = SimpleNamespace(
        id=12,
        platform="youtube",
        external_id="vid-12",
        title="Lesson Breakdown",
        url="https://example.com/lesson",
        published_at=datetime(2026, 3, 24, 10, 0, tzinfo=UTC),
        competitor=youtube_competitor,
    )

    monkeypatch.setattr(report_pipeline, "get_active_competitors", lambda *, user: [youtube_competitor, instagram_competitor])

    def fake_refresh_competitor(*, competitor, mode, captured_at, provider_fetch_cache=None):
        if competitor.external_id == "ig-5":
            raise CollectorError("Instagram timeout")
        return [item]

    monkeypatch.setattr(report_pipeline, "refresh_competitor", fake_refresh_competitor)

    preview = report_pipeline.build_setup_verification_preview(
        user=user,
        period_end=datetime(2026, 3, 24, 12, 0, tzinfo=UTC),
    )

    assert preview.payload["report_kind"] == "setup_verification"
    youtube_section = next(section for section in preview.payload["sections"] if section["platform"] == "youtube")
    instagram_section = next(section for section in preview.payload["sections"] if section["platform"] == "instagram")
    assert youtube_section["selected_competitors"] == 1
    assert youtube_section["successful_competitors"] == 1
    assert youtube_section["failed_competitors"] == 0
    assert youtube_section["examples"][0]["title"] == "Lesson Breakdown"
    assert instagram_section["selected_competitors"] == 1
    assert instagram_section["successful_competitors"] == 0
    assert instagram_section["failed_competitors"] == 1
    assert instagram_section["failures"] == [{"competitor": "Broken Gram", "reason": "Instagram timeout"}]
    assert "Проверка настройки завершена" in preview.text


@pytest.mark.django_db
def test_build_setup_verification_preview_keeps_all_three_collectible_sections(monkeypatch):
    user = TgUser.objects.create(tg_user_id=1004, tg_chat_id=1004, timezone_str="UTC")
    youtube_competitor = SimpleNamespace(id=10, platform="youtube", display_name="YT Hub", handle="yt-hub", external_id="yt-10")
    tiktok_competitor = SimpleNamespace(id=11, platform="tiktok", display_name="TT Hub", handle="tt-hub", external_id="tt-11")
    instagram_competitor = SimpleNamespace(id=12, platform="instagram", display_name="IG Hub", handle="ig-hub", external_id="ig-12")

    monkeypatch.setattr(
        report_pipeline,
        "get_active_competitors",
        lambda *, user: [youtube_competitor, tiktok_competitor, instagram_competitor],
    )

    def fake_refresh_competitor(*, competitor, mode, captured_at, provider_fetch_cache=None):
        content_type = {"youtube": "short", "tiktok": "video", "instagram": "reel"}[competitor.platform]
        titles = {
            "youtube": "English teacher lesson plans",
            "tiktok": "English tutor worksheet ideas",
            "instagram": "Reels for english teachers",
        }
        return [
            SimpleNamespace(
                id=100 + competitor.id,
                platform=competitor.platform,
                external_id=f"item-{competitor.id}",
                title=titles[competitor.platform],
                url=f"https://example.com/{competitor.platform}/{competitor.id}",
                published_at=datetime(2026, 3, 24, 10, 0, tzinfo=UTC),
                competitor=competitor,
                meta={"content_type": content_type},
            )
        ]

    monkeypatch.setattr(report_pipeline, "refresh_competitor", fake_refresh_competitor)

    preview = report_pipeline.build_setup_verification_preview(
        user=user,
        period_end=datetime(2026, 3, 24, 12, 0, tzinfo=UTC),
    )

    sections = {section["platform"]: section for section in preview.payload["sections"]}
    assert sections["youtube"]["successful_competitors"] == 1
    assert sections["tiktok"]["successful_competitors"] == 1
    assert sections["instagram"]["successful_competitors"] == 1
    assert sections["youtube"]["examples"][0]["content_type"] == "short"
    assert sections["instagram"]["examples"][0]["content_type"] == "reel"
    assert "English teacher lesson plans [Shorts]" in preview.text
    assert "English tutor worksheet ideas" in preview.text
    assert "Reels for english teachers [Reels]" in preview.text


@pytest.mark.django_db
def test_create_and_send_setup_verification_report_splits_long_messages(monkeypatch):
    user = TgUser.objects.create(tg_user_id=1005, tg_chat_id=1005, timezone_str="UTC")
    preview = ReportPreview(
        payload={"report_kind": "setup_verification", "sections": []},
        text=("A" * 3500) + "\n\n" + ("B" * 3500),
        section_counts={"youtube": 1},
        collection_failures=[],
    )
    sent: list[str] = []

    monkeypatch.setattr(report_pipeline, "build_setup_verification_preview", lambda **kwargs: preview)
    monkeypatch.setattr(report_pipeline, "send_message", lambda *, chat_id, text: sent.append(text) or {"message_id": len(sent)})

    result = report_pipeline.create_and_send_setup_verification_report(
        user=user,
        period_start=datetime(2026, 3, 23, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    assert len(sent) == 2
    assert result.telegram_result["message_ids"] == [1, 2]
