from __future__ import annotations

from datetime import UTC, datetime

from django.test import override_settings

from tracking.models import Competitor, Platform, Report, TgUser, UserCompetitor
from tracking.services.suggested_competitors import (
    build_youtube_suggested_competitors_payload,
    render_youtube_suggested_competitors_text,
)


def _lane_payload(
    *,
    channel_id: str,
    channel_title: str,
    score: float,
    video_suffix: str,
    views: int = 5000,
    likes: int = 180,
    comments: int = 20,
    traction: float = 0.14,
) -> dict:
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
                "views": views,
                "likes": likes,
                "comments": comments,
                "supplemental_score": score,
                "supplemental_survival_reason": "deterministic_rank_pass",
                "supplemental_ranking_factors": {
                    "instructional_markers": 0.18,
                    "query_phrase_match": 0.18,
                    "traction": traction,
                },
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
    assert payload["items"][0]["strongest_supporting_video_title"] == "Useful idea current"
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


def test_blocked_inactive_competitor_is_not_suggested(db):
    user = TgUser.objects.create(tg_user_id=404, tg_chat_id=404)
    blocked = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="chan-blocked",
        handle="chan_blocked",
        url="https://www.youtube.com/channel/chan-blocked",
        display_name="Blocked Coach",
    )
    UserCompetitor.objects.create(user=user, competitor=blocked, is_active=False)
    Report.objects.create(
        user=user,
        period_start=datetime(2026, 3, 28, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 29, 0, 0, tzinfo=UTC),
        status="sent",
        payload={"supplemental": {"youtube_topic_video": _lane_payload(channel_id="chan-blocked", channel_title="Blocked Coach", score=0.66, video_suffix="hist")}},
    )

    payload = build_youtube_suggested_competitors_payload(
        user=user,
        current_lane_payload=_lane_payload(channel_id="chan-blocked", channel_title="Blocked Coach", score=0.7, video_suffix="current"),
    )

    assert payload["items"] == []
    assert payload["diagnostics"]["dropped_blocked"] == 1


def test_repeated_creator_with_weak_supporting_videos_is_filtered_out(db):
    user = TgUser.objects.create(tg_user_id=405, tg_chat_id=405)
    Report.objects.create(
        user=user,
        period_start=datetime(2026, 3, 28, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 29, 0, 0, tzinfo=UTC),
        status="sent",
        payload={
            "supplemental": {
                "youtube_topic_video": _lane_payload(
                    channel_id="chan-weak",
                    channel_title="Weak Coach",
                    score=0.62,
                    video_suffix="hist",
                    views=400,
                    likes=2,
                    comments=1,
                    traction=0.04,
                )
            }
        },
    )

    payload = build_youtube_suggested_competitors_payload(
        user=user,
        current_lane_payload=_lane_payload(
            channel_id="chan-weak",
            channel_title="Weak Coach",
            score=0.64,
            video_suffix="current",
            views=450,
            likes=3,
            comments=1,
            traction=0.05,
        ),
    )

    assert payload["items"] == []
    assert payload["diagnostics"]["dropped_weak_supporting_evidence"] == 1


def test_repeated_creator_with_strong_supporting_video_is_suggested(db):
    user = TgUser.objects.create(tg_user_id=406, tg_chat_id=406)
    Report.objects.create(
        user=user,
        period_start=datetime(2026, 3, 28, 0, 0, tzinfo=UTC),
        period_end=datetime(2026, 3, 29, 0, 0, tzinfo=UTC),
        status="sent",
        payload={"supplemental": {"youtube_topic_video": _lane_payload(channel_id="chan-strong", channel_title="Strong Coach", score=0.61, video_suffix="hist")}},
    )

    payload = build_youtube_suggested_competitors_payload(
        user=user,
        current_lane_payload=_lane_payload(
            channel_id="chan-strong",
            channel_title="Strong Coach",
            score=0.72,
            video_suffix="current",
            views=9000,
            likes=360,
            comments=60,
            traction=0.16,
        ),
    )

    assert payload["diagnostics"]["final_suggestions"] == 1
    assert payload["items"][0]["channel_id"] == "chan-strong"
    assert payload["items"][0]["strongest_supporting_video_score"] == 0.72


def test_render_youtube_suggested_competitors_text_returns_none_without_items():
    assert render_youtube_suggested_competitors_text(suggestion_payload={"items": []}) is None


@override_settings(REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MAX_ITEMS=1)
def test_suggested_competitor_diagnostics_include_observability_counters(db):
    user = TgUser.objects.create(tg_user_id=403, tg_chat_id=403)
    active = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="chan-active",
        handle="chan_active",
        url="https://www.youtube.com/channel/chan-active",
        display_name="Active Coach",
    )
    UserCompetitor.objects.create(user=user, competitor=active, is_active=True)
    for report_id, channel_id, title, score in (
        (1, "chan-repeat", "Repeat Coach", 0.61),
        (2, "chan-limit", "Limit Coach", 0.63),
    ):
        Report.objects.create(
            user=user,
            period_start=datetime(2026, 3, 20 + report_id, 0, 0, tzinfo=UTC),
            period_end=datetime(2026, 3, 21 + report_id, 0, 0, tzinfo=UTC),
            status="sent",
            payload={"supplemental": {"youtube_topic_video": _lane_payload(channel_id=channel_id, channel_title=title, score=score, video_suffix=f"hist-{report_id}")}},
        )

    payload = build_youtube_suggested_competitors_payload(
        user=user,
        current_lane_payload={
            "source": "supplemental_topic_video",
            "candidates": [
                {
                    "video_id": "curr-repeat-1",
                    "url": "https://www.youtube.com/watch?v=curr-repeat-1",
                    "title": "Repeat current 1",
                    "channel_id": "chan-repeat",
                    "channel_title": "Repeat Coach",
                    "matched_queries": ["spoken english"],
                    "views": 5000,
                    "likes": 180,
                    "comments": 20,
                    "supplemental_score": 0.66,
                    "supplemental_survival_reason": "deterministic_rank_pass",
                    "supplemental_ranking_factors": {
                        "instructional_markers": 0.18,
                        "query_phrase_match": 0.18,
                        "traction": 0.14,
                    },
                },
                {
                    "video_id": "curr-repeat-2",
                    "url": "https://www.youtube.com/watch?v=curr-repeat-2",
                    "title": "Repeat current 2",
                    "channel_id": "chan-repeat",
                    "channel_title": "Repeat Coach",
                    "matched_queries": ["spoken english"],
                    "views": 4800,
                    "likes": 160,
                    "comments": 18,
                    "supplemental_score": 0.65,
                    "supplemental_survival_reason": "deterministic_rank_pass",
                    "supplemental_ranking_factors": {
                        "instructional_markers": 0.16,
                        "query_phrase_match": 0.18,
                        "traction": 0.13,
                    },
                },
                {
                    "video_id": "curr-limit",
                    "url": "https://www.youtube.com/watch?v=curr-limit",
                    "title": "Limit current",
                    "channel_id": "chan-limit",
                    "channel_title": "Limit Coach",
                    "matched_queries": ["spoken english"],
                    "views": 5100,
                    "likes": 200,
                    "comments": 25,
                    "supplemental_score": 0.67,
                    "supplemental_survival_reason": "deterministic_rank_pass",
                    "supplemental_ranking_factors": {
                        "instructional_markers": 0.18,
                        "query_phrase_match": 0.18,
                        "traction": 0.14,
                    },
                },
                {
                    "video_id": "curr-active",
                    "url": "https://www.youtube.com/watch?v=curr-active",
                    "title": "Active current",
                    "channel_id": "chan-active",
                    "channel_title": "Active Coach",
                    "matched_queries": ["spoken english"],
                    "views": 6500,
                    "likes": 210,
                    "comments": 30,
                    "supplemental_score": 0.72,
                    "supplemental_survival_reason": "deterministic_rank_pass",
                    "supplemental_ranking_factors": {
                        "instructional_markers": 0.2,
                        "query_phrase_match": 0.18,
                        "traction": 0.15,
                    },
                },
                {
                    "video_id": "curr-single",
                    "url": "https://www.youtube.com/watch?v=curr-single",
                    "title": "Single current",
                    "channel_id": "chan-single",
                    "channel_title": "Single Coach",
                    "matched_queries": ["spoken english"],
                    "views": 7000,
                    "likes": 220,
                    "comments": 32,
                    "supplemental_score": 0.88,
                    "supplemental_survival_reason": "deterministic_rank_pass",
                    "supplemental_ranking_factors": {
                        "instructional_markers": 0.22,
                        "query_phrase_match": 0.18,
                        "traction": 0.16,
                    },
                },
            ],
        },
    )

    diagnostics = payload["diagnostics"]
    assert diagnostics["suggestions_considered"] == 4
    assert diagnostics["suggestions_generated"] == 2
    assert diagnostics["suggestions_sent"] == 0
    assert diagnostics["dropped_blocked"] == 0
    assert diagnostics["dropped_already_active"] == 1
    assert diagnostics["dropped_not_repeated"] == 1
    assert diagnostics["dropped_dedup"] == 1
    assert diagnostics["dropped_limit"] == 1
    assert diagnostics["suppressed_by_run_cap"] == 0
    assert diagnostics["suppressed_by_cooldown"] == 0
    assert diagnostics["dropped_weak_supporting_evidence"] == 0
    assert payload["acceptance"] == {"clicked_add": 0, "added": 0, "already_active": 0, "events": []}
