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
    monkeypatch.setattr(report_pipeline, "compute_competitor_baseline", lambda *, competitor, now: {"median": 1})
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
