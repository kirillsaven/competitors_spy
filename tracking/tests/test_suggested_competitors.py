from __future__ import annotations

from datetime import UTC, datetime

from tracking.models import Competitor, Platform, Report, TgUser, UserCompetitor
from tracking.services.suggested_competitors import (
    build_youtube_suggested_competitors_payload,
    render_youtube_suggested_competitors_text,
)


def _lane_payload(*, channel_id: str, channel_title: str, score: float, video_suffix: str) -> dict:
    return {
        "source": "supplemental_topic_video",
        "candidates": [
            {
                "video_id": f"video-{video_suffix}",
                "url": f"https://www.youtube.com/watch?v=video-{video_suffix}",
                "title": f"Useful idea {video_suffix}",
                "channel_id": channel_id,
                "channel_title": channel_title,
                "matched_queries": ["spoken english"],
                "supplemental_score": score,
            }
        ],
    }


def test_repeated_supplemental_creator_becomes_suggested_competitor(db):
    user = TgUser.objects.create(tg_user_id=401, tg_chat_id=401)
    Report.objects.create(
        user=user,
        period_start=datetime(2026, 3, 28, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 29, 0, 0, tzinfo=UTC),
        status="sent",
        payload={"supplemental": {"youtube_topic_video": _lane_payload(channel_id="chan-repeat", channel_title="Topic Coach", score=0.61, video_suffix="hist")}},
    )

    payload = build_youtube_suggested_competitors_payload(
        user=user,
        current_lane_payload=_lane_payload(channel_id="chan-repeat", channel_title="Topic Coach", score=0.64, video_suffix="current"),
    )

    assert payload["diagnostics"]["final_suggestions"] == 1
    assert payload["items"][0]["channel_id"] == "chan-repeat"
    assert payload["items"][0]["appearance_count"] == 2
    assert payload["items"][0]["suggestion_reason"] == "repeated_supplemental_creator"


def test_already_active_competitor_is_not_suggested(db):
    user = TgUser.objects.create(tg_user_id=402, tg_chat_id=402)
    competitor = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="chan-active",
        handle="chan_active",
        url="https://www.youtube.com/channel/chan-active",
        display_name="Active Coach",
    )
    UserCompetitor.objects.create(user=user, competitor=competitor, is_active=True)
    Report.objects.create(
        user=user,
        period_start=datetime(2026, 3, 28, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 29, 0, 0, tzinfo=UTC),
        status="sent",
        payload={"supplemental": {"youtube_topic_video": _lane_payload(channel_id="chan-active", channel_title="Active Coach", score=0.61, video_suffix="hist")}},
    )

    payload = build_youtube_suggested_competitors_payload(
        user=user,
        current_lane_payload=_lane_payload(channel_id="chan-active", channel_title="Active Coach", score=0.64, video_suffix="current"),
    )

    assert payload["items"] == []
    assert payload["diagnostics"]["dropped_already_active"] == 1


def test_render_youtube_suggested_competitors_text_returns_none_without_items():
    assert render_youtube_suggested_competitors_text(suggestion_payload={"items": []}) is None
