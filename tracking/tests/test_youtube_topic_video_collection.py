from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from django.test import override_settings

from tracking.services import youtube_topic_video_collection as collection


@override_settings(YT_SUPPLEMENTAL_MAX_QUERIES=4, YT_SUPPLEMENTAL_SHORTS_ONLY=True)
def test_build_youtube_topic_video_queries_returns_bounded_deduped_set():
    queries = collection.build_youtube_topic_video_queries(
        niche_keywords=[
            "spoken english",
            "spoken english",
            "english lessons",
            "english grammar",
            "english vocabulary",
        ],
        linked_accounts=[
            SimpleNamespace(
                display_name="English with Anna",
                handle="anna_english",
                meta={"description": "spoken english lessons for beginners"},
            )
        ],
        competitors=[
            SimpleNamespace(
                display_name="English Coach",
                handle="english_coach",
                meta={"description": "english grammar and speaking practice"},
            )
        ],
    )

    assert len(queries) == 4
    assert len({query.lower() for query in queries}) == 4
    assert "spoken english" in queries


@override_settings(
    YT_SUPPLEMENTAL_MAX_QUERIES=3,
    YT_SUPPLEMENTAL_MAX_RESULTS_PER_QUERY=2,
    YT_SUPPLEMENTAL_MAX_SEARCH_PAGES_PER_QUERY=1,
    YT_SUPPLEMENTAL_MAX_HYDRATED_VIDEOS=2,
    YT_SUPPLEMENTAL_MAX_AGE_DAYS=30,
    YT_SUPPLEMENTAL_SHORTS_ONLY=True,
)
def test_collect_youtube_topic_video_candidates_collapses_duplicates_and_keeps_provenance():
    class FakeClient:
        def __init__(self):
            self.search_calls: list[dict[str, object]] = []
            self.hydrate_ids: list[str] = []

        def search_videos_page(self, *, q, max_results, page_token=None, published_after=None, short_duration_only=False):
            self.search_calls.append(
                {
                    "query": q,
                    "max_results": max_results,
                    "page_token": page_token,
                    "published_after": published_after,
                    "short_duration_only": short_duration_only,
                }
            )
            if q == "spoken english":
                return [
                    {
                        "id": {"videoId": "vid-1"},
                        "snippet": {"channelId": "chan-1", "channelTitle": "Coach One"},
                    },
                    {
                        "id": {"videoId": "vid-2"},
                        "snippet": {"channelId": "chan-2", "channelTitle": "Coach Two"},
                    },
                ], None
            if q == "english lessons":
                return [
                    {
                        "id": {"videoId": "vid-2"},
                        "snippet": {"channelId": "chan-2", "channelTitle": "Coach Two"},
                    },
                    {
                        "id": {"videoId": "vid-3"},
                        "snippet": {"channelId": "chan-3", "channelTitle": "Coach Three"},
                    },
                ], None
            return [], None

        def videos_list(self, *, ids, part):
            self.hydrate_ids = list(ids)
            mapping = {
                "vid-1": {
                    "id": "vid-1",
                    "snippet": {
                        "title": "Spoken English tips",
                        "description": "lesson examples",
                        "publishedAt": "2026-03-20T12:00:00Z",
                    },
                    "statistics": {"viewCount": "1200", "likeCount": "40", "commentCount": "5"},
                    "contentDetails": {"duration": "PT45S"},
                },
                "vid-2": {
                    "id": "vid-2",
                    "snippet": {
                        "title": "English lesson mistakes",
                        "description": "mistakes guide",
                        "publishedAt": "2026-03-21T12:00:00Z",
                    },
                    "statistics": {"viewCount": "2200", "likeCount": "65", "commentCount": "8"},
                    "contentDetails": {"duration": "PT58S"},
                },
            }
            return [mapping[video_id] for video_id in ids]

    fake_client = FakeClient()
    result = collection.collect_youtube_topic_video_candidates(
        niche_keywords=["spoken english", "english lessons"],
        now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
        client=fake_client,
    )

    assert fake_client.hydrate_ids == ["vid-1", "vid-2"]
    assert [candidate.video_id for candidate in result.candidates] == ["vid-1", "vid-2"]
    assert result.candidates[1].matched_queries == ("spoken english", "english lessons")
    assert result.candidates[1].hit_count == 2
    assert result.candidates[1].first_seen_rank == 2
    assert result.diagnostics["search_hits_total"] == 4
    assert result.diagnostics["unique_video_hits"] == 3
    assert result.diagnostics["dropped_by_dedup"] == 1
    assert result.diagnostics["dropped_by_hydration_budget"] == 1
    assert result.diagnostics["hydration_requested"] == 2
    assert result.diagnostics["final_candidates"] == 2
    assert result.candidates[0].supplemental_score >= result.candidates[1].supplemental_score
    assert result.candidates[0].supplemental_survival_reason == "deterministic_rank_pass"
    assert "supplemental_ranking_factors" in result.candidates[0].to_payload()
    query_stats = {entry["query"]: entry for entry in result.diagnostics["query_stats"]}
    assert query_stats["spoken english"]["final_candidates"] == 2
    assert query_stats["english lessons"]["final_candidates"] == 1


@override_settings(
    YT_SUPPLEMENTAL_MAX_QUERIES=2,
    YT_SUPPLEMENTAL_MAX_RESULTS_PER_QUERY=2,
    YT_SUPPLEMENTAL_MAX_SEARCH_PAGES_PER_QUERY=1,
    YT_SUPPLEMENTAL_MAX_HYDRATED_VIDEOS=5,
    YT_SUPPLEMENTAL_MAX_AGE_DAYS=30,
    YT_SUPPLEMENTAL_SHORTS_ONLY=True,
)
def test_collect_youtube_topic_video_candidates_respects_bounded_search_budget():
    class FakeClient:
        def __init__(self):
            self.search_calls: list[dict[str, object]] = []

        def search_videos_page(self, *, q, max_results, page_token=None, published_after=None, short_duration_only=False):
            self.search_calls.append({"query": q, "page_token": page_token, "max_results": max_results})
            return [
                {
                    "id": {"videoId": f"{q}-1"},
                    "snippet": {"channelId": f"{q}-chan", "channelTitle": q},
                }
            ], "next-page"

        def videos_list(self, *, ids, part):
            return [
                {
                    "id": video_id,
                    "snippet": {
                        "title": video_id,
                        "description": "desc",
                        "publishedAt": "2026-03-20T12:00:00Z",
                    },
                    "statistics": {"viewCount": "1200"},
                    "contentDetails": {"duration": "PT50S"},
                }
                for video_id in ids
            ]

    fake_client = FakeClient()
    result = collection.collect_youtube_topic_video_candidates(
        niche_keywords=["spoken english", "english lessons", "english grammar"],
        now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
        client=fake_client,
    )

    assert len(fake_client.search_calls) == 2
    assert [call["page_token"] for call in fake_client.search_calls] == [None, None]
    assert result.diagnostics["queries_executed"] == 2
    assert result.diagnostics["max_search_pages_per_query"] == 1
    assert result.diagnostics["hydration_requested"] == 2


@override_settings(
    YT_SUPPLEMENTAL_MAX_QUERIES=1,
    YT_SUPPLEMENTAL_MAX_RESULTS_PER_QUERY=3,
    YT_SUPPLEMENTAL_MAX_SEARCH_PAGES_PER_QUERY=1,
    YT_SUPPLEMENTAL_MAX_HYDRATED_VIDEOS=3,
    YT_SUPPLEMENTAL_MAX_AGE_DAYS=30,
    YT_SUPPLEMENTAL_SHORTS_ONLY=True,
)
def test_collect_youtube_topic_video_candidates_populates_filter_diagnostics():
    class FakeClient:
        def search_videos_page(self, *, q, max_results, page_token=None, published_after=None, short_duration_only=False):
            return [
                {
                    "id": {"videoId": "missing-date"},
                    "snippet": {"channelId": "chan-1", "channelTitle": "Coach One"},
                },
                {
                    "id": {"videoId": "old-video"},
                    "snippet": {"channelId": "chan-2", "channelTitle": "Coach Two"},
                },
                {
                    "id": {"videoId": "long-video"},
                    "snippet": {"channelId": "chan-3", "channelTitle": "Coach Three"},
                },
            ], None

        def videos_list(self, *, ids, part):
            return [
                {
                    "id": "missing-date",
                    "snippet": {"title": "No date", "description": "desc"},
                    "statistics": {"viewCount": "1000"},
                    "contentDetails": {"duration": "PT40S"},
                },
                {
                    "id": "old-video",
                    "snippet": {
                        "title": "Old video",
                        "description": "desc",
                        "publishedAt": "2026-02-01T12:00:00Z",
                    },
                    "statistics": {"viewCount": "1000"},
                    "contentDetails": {"duration": "PT40S"},
                },
                {
                    "id": "long-video",
                    "snippet": {
                        "title": "Long video",
                        "description": "desc",
                        "publishedAt": "2026-03-20T12:00:00Z",
                    },
                    "statistics": {"viewCount": "1000"},
                    "contentDetails": {"duration": "PT5M"},
                },
            ]

    result = collection.collect_youtube_topic_video_candidates(
        niche_keywords=["spoken english"],
        now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
        client=FakeClient(),
    )

    assert result.candidates == ()
    assert result.diagnostics["filtered_missing_published_at"] == 1
    assert result.diagnostics["filtered_too_old"] == 1
    assert result.diagnostics["filtered_non_short"] == 1


@override_settings(
    YT_SUPPLEMENTAL_MAX_QUERIES=2,
    YT_SUPPLEMENTAL_MAX_RESULTS_PER_QUERY=2,
    YT_SUPPLEMENTAL_MAX_SEARCH_PAGES_PER_QUERY=1,
    YT_SUPPLEMENTAL_MAX_HYDRATED_VIDEOS=5,
    YT_SUPPLEMENTAL_MAX_AGE_DAYS=30,
    YT_SUPPLEMENTAL_SHORTS_ONLY=True,
)
def test_generic_query_noise_candidate_gets_filtered():
    class FakeClient:
        def search_videos_page(self, *, q, max_results, page_token=None, published_after=None, short_duration_only=False):
            return [
                {
                    "id": {"videoId": "noise-1"},
                    "snippet": {"channelId": "chan-1", "channelTitle": "Noise Channel"},
                }
            ], None

        def videos_list(self, *, ids, part):
            assert ids == ["noise-1"]
            return [
                {
                    "id": "noise-1",
                    "snippet": {
                        "title": "Лучшие lyrics для english songs",
                        "description": "music cover and lyrics shorts",
                        "publishedAt": "2026-03-20T12:00:00Z",
                    },
                    "statistics": {"viewCount": "23060"},
                    "contentDetails": {"duration": "PT45S"},
                }
            ]

    result = collection.collect_youtube_topic_video_candidates(
        niche_keywords=["репетитор", "английскому shorts"],
        now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
        client=FakeClient(),
    )

    assert result.candidates == ()
    assert result.diagnostics["raw_candidates_before_ranking"] == 1
    assert result.diagnostics["dropped_by_generic_query_weakness"] == 1


@override_settings(
    YT_SUPPLEMENTAL_MAX_QUERIES=2,
    YT_SUPPLEMENTAL_MAX_RESULTS_PER_QUERY=2,
    YT_SUPPLEMENTAL_MAX_SEARCH_PAGES_PER_QUERY=1,
    YT_SUPPLEMENTAL_MAX_HYDRATED_VIDEOS=5,
    YT_SUPPLEMENTAL_MAX_AGE_DAYS=30,
    YT_SUPPLEMENTAL_SHORTS_ONLY=True,
)
def test_strong_topical_candidate_outranks_generic_noisy_candidate():
    class FakeClient:
        def search_videos_page(self, *, q, max_results, page_token=None, published_after=None, short_duration_only=False):
            return [
                {
                    "id": {"videoId": "strong-1"},
                    "snippet": {"channelId": "chan-1", "channelTitle": "Coach One"},
                },
                {
                    "id": {"videoId": "noise-2"},
                    "snippet": {"channelId": "chan-2", "channelTitle": "Noise Channel"},
                },
            ], None

        def videos_list(self, *, ids, part):
            mapping = {
                "strong-1": {
                    "id": "strong-1",
                    "snippet": {
                        "title": "Разговорный английский для взрослых: 5 фраз для small talk",
                        "description": "урок, примеры и диалог для практики",
                        "publishedAt": "2026-03-26T12:00:00Z",
                    },
                    "statistics": {"viewCount": "5400"},
                    "contentDetails": {"duration": "PT49S"},
                },
                "noise-2": {
                    "id": "noise-2",
                    "snippet": {
                        "title": "English shorts mix",
                        "description": "funny music lyrics compilation",
                        "publishedAt": "2026-03-26T12:00:00Z",
                    },
                    "statistics": {"viewCount": "15000"},
                    "contentDetails": {"duration": "PT45S"},
                },
            }
            return [mapping[video_id] for video_id in ids]

    result = collection.collect_youtube_topic_video_candidates(
        niche_keywords=["разговорный английский для взрослых", "small talk english"],
        now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
        client=FakeClient(),
    )

    assert [candidate.video_id for candidate in result.candidates] == ["strong-1"]
    assert result.diagnostics["dropped_by_off_topic_penalty"] == 1 or result.diagnostics["dropped_by_low_supplemental_score"] == 1
    assert result.candidates[0].supplemental_ranking_factors["niche_phrase_match"] > 0


@override_settings(
    YT_SUPPLEMENTAL_MAX_QUERIES=1,
    YT_SUPPLEMENTAL_MAX_RESULTS_PER_QUERY=2,
    YT_SUPPLEMENTAL_MAX_SEARCH_PAGES_PER_QUERY=1,
    YT_SUPPLEMENTAL_MAX_HYDRATED_VIDEOS=5,
    YT_SUPPLEMENTAL_MAX_AGE_DAYS=30,
    YT_SUPPLEMENTAL_SHORTS_ONLY=True,
)
def test_phrase_match_and_instructional_format_survives_with_ranking_factors():
    class FakeClient:
        def search_videos_page(self, *, q, max_results, page_token=None, published_after=None, short_duration_only=False):
            return [
                {
                    "id": {"videoId": "teach-1"},
                    "snippet": {"channelId": "chan-1", "channelTitle": "Coach One"},
                }
            ], None

        def videos_list(self, *, ids, part):
            return [
                {
                    "id": "teach-1",
                    "snippet": {
                        "title": "Разговорный английский для взрослых: урок и диалог",
                        "description": "how to use these phrases, examples, practice checklist",
                        "publishedAt": "2026-03-28T12:00:00Z",
                    },
                    "statistics": {"viewCount": "3100", "likeCount": "80"},
                    "contentDetails": {"duration": "PT35S"},
                }
            ]

    result = collection.collect_youtube_topic_video_candidates(
        niche_keywords=["разговорный английский для взрослых"],
        now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
        client=FakeClient(),
    )

    assert [candidate.video_id for candidate in result.candidates] == ["teach-1"]
    candidate = result.candidates[0]
    assert candidate.supplemental_score >= 0.35
    assert candidate.supplemental_ranking_factors["niche_phrase_match"] > 0
    assert candidate.supplemental_ranking_factors["instructional_markers"] > 0
    assert candidate.supplemental_ranking_factors["adaptable_format_markers"] > 0
    payload = candidate.to_payload()
    assert payload["supplemental_score"] == candidate.supplemental_score
    assert payload["supplemental_survival_reason"] == "deterministic_rank_pass"


@override_settings(
    YT_SUPPLEMENTAL_MAX_QUERIES=1,
    YT_SUPPLEMENTAL_MAX_RESULTS_PER_QUERY=2,
    YT_SUPPLEMENTAL_MAX_SEARCH_PAGES_PER_QUERY=1,
    YT_SUPPLEMENTAL_MAX_HYDRATED_VIDEOS=5,
    YT_SUPPLEMENTAL_MAX_AGE_DAYS=30,
    YT_SUPPLEMENTAL_SHORTS_ONLY=True,
)
def test_off_topic_candidate_rejected():
    class FakeClient:
        def search_videos_page(self, *, q, max_results, page_token=None, published_after=None, short_duration_only=False):
            return [
                {
                    "id": {"videoId": "off-1"},
                    "snippet": {"channelId": "chan-1", "channelTitle": "Fun Channel"},
                }
            ], None

        def videos_list(self, *, ids, part):
            return [
                {
                    "id": "off-1",
                    "snippet": {
                        "title": "Celebrity prank challenge",
                        "description": "funny vlog reaction compilation",
                        "publishedAt": "2026-03-29T12:00:00Z",
                    },
                    "statistics": {"viewCount": "60000"},
                    "contentDetails": {"duration": "PT40S"},
                }
            ]

    result = collection.collect_youtube_topic_video_candidates(
        niche_keywords=["spoken english for adults"],
        now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
        client=FakeClient(),
    )

    assert result.candidates == ()
    assert result.diagnostics["dropped_by_off_topic_penalty"] == 1
