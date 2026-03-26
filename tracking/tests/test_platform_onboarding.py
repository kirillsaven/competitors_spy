from __future__ import annotations

import pytest

from tracking.adapters.base import SeedResolution
from tracking.models import Platform
from tracking.services import platform_onboarding
from tracking.services import seed_resolver
from tracking.services.setup_retry_cache import clear_retry_cache
from tracking.services.setup_runtime import PLATFORM_STATE_UNAVAILABLE, SetupRunContext, get_platform_state


def _seed(
    *,
    platform: str,
    external_id: str,
    handle: str | None,
    title: str | None,
    description: str | None = None,
    url: str | None = None,
) -> SeedResolution:
    return SeedResolution(
        platform=platform,
        external_id=external_id,
        handle=handle,
        url=url or f"https://example.com/{external_id}",
        title=title,
        description=description,
        uploads_playlist_id=None,
    )


def test_get_recent_seed_content_texts_reads_tiktok_captions(monkeypatch):
    sample_items = [
        {
            "id": "7353646097262202145",
            "text": "Fast break finish #nba",
            "createTimeISO": "2024-04-03T14:22:40.000Z",
            "authorMeta": {
                "id": "7353570794285417504",
                "name": "apifytech",
                "nickName": "Apify Tech",
            },
            "webVideoUrl": "https://www.tiktok.com/@apifytech/video/7353646097262202145",
            "videoMeta": {"duration": 59},
            "diggCount": 725,
            "shareCount": 30,
            "playCount": 83900,
            "commentCount": 10,
        }
    ]

    class FakeClient:
        def fetch_profile_feed(self, *, handle, results_per_page):
            assert handle == "apifytech"
            assert results_per_page == 5
            return sample_items

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_get_tiktok_client", lambda: FakeClient())

    texts = platform_onboarding.get_recent_seed_content_texts(
        seed=_seed(
            platform=Platform.TIKTOK,
            external_id="tt-1",
            handle="apifytech",
            url="https://www.tiktok.com/@apifytech",
            title="Apify Tech",
            description="Automation",
        ),
        n=5,
    )

    assert texts == ["Fast break finish #nba"]


def test_get_recent_seed_content_texts_reads_instagram_reel_captions(monkeypatch):
    sample_profiles = [
        {
            "id": "ig-1",
            "username": "nasa",
            "url": "https://www.instagram.com/nasa/",
            "latestPosts": [
                {
                    "id": "post-1",
                    "productType": "clips",
                    "url": "https://www.instagram.com/reel/post-1/",
                    "caption": "Mars update",
                    "timestamp": "2024-07-03T10:30:00.000Z",
                    "videoViewCount": 1500,
                },
                {
                    "id": "post-2",
                    "productType": "feed",
                    "url": "https://www.instagram.com/p/post-2/",
                    "description": "Moon update",
                    "timestamp": "2024-07-03T10:35:00.000Z",
                    "videoViewCount": 1800,
                },
            ],
        }
    ]

    class FakeClient:
        def fetch_profiles(self, *, inputs):
            assert inputs == ["https://www.instagram.com/nasa/"]
            return sample_profiles

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_get_instagram_client", lambda: FakeClient())

    texts = platform_onboarding.get_recent_seed_content_texts(
        seed=_seed(
            platform=Platform.INSTAGRAM,
            external_id="ig-1",
            handle="nasa",
            url="https://www.instagram.com/nasa/",
            title="NASA",
            description="Space agency",
        ),
        n=5,
    )

    assert texts == ["Mars update"]


def test_discover_instagram_competitors_performs_real_query_search(monkeypatch):
    search_calls: list[str] = []

    class FakeClient:
        def search_profiles(self, *, query):
            search_calls.append(query)
            if query == "english teachers":
                return [
                    {"id": "ig-1", "username": "teacher_hub", "full_name": "Teacher Hub", "is_verified": True},
                    {"id": "ig-2", "username": "lessonlab", "full_name": "Lesson Lab"},
                ]
            return [
                {"id": "ig-1", "username": "teacher_hub", "full_name": "Teacher Hub", "is_verified": True},
                {"id": "ig-3", "username": "teachernotes", "full_name": "Teacher Notes"},
            ]

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_get_instagram_client", lambda: FakeClient())

    candidates = platform_onboarding.discover_instagram_competitors(
        keywords=["english teachers", "teacher groups"],
        max_candidates=10,
    )

    assert search_calls == ["teacher groups", "english teachers"]
    assert [candidate.external_id for candidate in candidates] == ["ig-1", "ig-3", "ig-2"]
    assert candidates[0].reason == "search: english teachers, teacher groups"


def test_discover_tiktok_competitors_performs_real_query_search(monkeypatch):
    search_calls: list[str] = []

    class FakeClient:
        def search_profiles(self, *, query):
            search_calls.append(query)
            if query == "english teachers":
                return [
                    {"id": "tt-1", "name": "teacherhub", "nickName": "Teacher Hub", "signature": "English teacher groups", "profileUrl": "https://www.tiktok.com/@teacherhub", "fans": 5000},
                    {"id": "tt-2", "name": "lessonlab", "nickName": "Lesson Lab", "signature": "Lesson planning for tutors", "profileUrl": "https://www.tiktok.com/@lessonlab", "fans": 3000},
                ]
            return [
                {"id": "tt-1", "name": "teacherhub", "nickName": "Teacher Hub", "signature": "English teacher groups", "profileUrl": "https://www.tiktok.com/@teacherhub", "fans": 5000},
                {"id": "tt-3", "name": "teachernotes", "nickName": "Teacher Notes", "signature": "Tutor notes and lesson ideas", "profileUrl": "https://www.tiktok.com/@teachernotes", "fans": 2000},
            ]

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_get_tiktok_client", lambda: FakeClient())

    candidates = platform_onboarding.discover_tiktok_competitors(
        keywords=["english teachers", "teacher groups"],
        max_candidates=10,
    )

    assert search_calls == ["teacher groups", "english teachers"]
    assert [candidate.external_id for candidate in candidates] == ["tt-1", "tt-3", "tt-2"]
    assert candidates[0].reason == "search: english teachers, teacher groups"


def test_discover_competitors_for_onboarding_reports_empty_after_attempted_search(monkeypatch):
    monkeypatch.setattr(platform_onboarding, "_discover_youtube_search_candidates", lambda **kwargs: [])
    monkeypatch.setattr(platform_onboarding, "_search_instagram_candidates_raw", lambda **kwargs: [])
    monkeypatch.setattr(platform_onboarding, "_search_tiktok_candidates_raw", lambda **kwargs: [])

    outcome = platform_onboarding.discover_competitors_for_onboarding(
        keywords=["english teachers", "teacher groups"],
        seed=_seed(
            platform=Platform.INSTAGRAM,
            external_id="ig-seed",
            handle="dariapancho",
            title="Дарья Панчо",
            description="English tutor",
            url="https://www.instagram.com/dariapancho/",
        ),
        competitors=[],
        linked_accounts=[],
        max_youtube_search_calls=3,
        max_candidates_per_platform=20,
    )

    assert outcome.candidates == []
    assert outcome.notes == [
        "YouTube: EMPTY — по текущим поисковым фразам поиск был выполнен, но кандидаты не найдены.",
        "Instagram: EMPTY — по текущим поисковым фразам поиск был выполнен, но кандидаты не найдены.",
        "TikTok: EMPTY — по текущим поисковым фразам поиск был выполнен, но кандидаты не найдены.",
    ]


def test_discover_competitors_for_onboarding_ranks_and_dedupes_candidates(monkeypatch):
    monkeypatch.setattr(
        platform_onboarding,
        "_discover_youtube_search_candidates",
        lambda **kwargs: [
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.YOUTUBE,
                external_id="yt-seed",
                handle="dariapancho",
                url="https://www.youtube.com/@dariapancho",
                display_name="Daria Pancho",
                description="Seed account",
                query_hits={"english teachers"},
                metadata={"rank_hint": 1000},
            ),
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.YOUTUBE,
                external_id="yt-1",
                handle="teacherhub",
                url="https://www.youtube.com/@teacherhub",
                display_name="Teacher Hub",
                description="English teacher groups and tutor notes",
                query_hits={"english teachers", "teacher groups"},
                metadata={"rank_hint": 5000},
            ),
        ],
    )
    monkeypatch.setattr(
        platform_onboarding,
        "_search_instagram_candidates_raw",
        lambda **kwargs: [
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.INSTAGRAM,
                external_id="ig-1",
                handle="teacherhub",
                url="https://www.instagram.com/teacherhub/",
                display_name="Teacher Hub",
                description="English teacher groups and tutor notes",
                query_hits={"english teachers"},
            )
        ],
    )
    monkeypatch.setattr(
        platform_onboarding,
        "_search_tiktok_candidates_raw",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("provider timeout")),
    )
    monkeypatch.setattr(
        platform_onboarding,
        "_collector_aware_candidates",
        lambda **kwargs: (kwargs["candidates"], ""),
    )

    outcome = platform_onboarding.discover_competitors_for_onboarding(
        keywords=["english teachers", "teacher groups"],
        seed=_seed(
            platform=Platform.INSTAGRAM,
            external_id="ig-seed",
            handle="dariapancho",
            title="Дарья Панчо",
            description="English tutor",
            url="https://www.instagram.com/dariapancho/",
        ),
        competitors=[],
        linked_accounts=[
            _seed(
                platform=Platform.YOUTUBE,
                external_id="yt-seed",
                handle="dariapancho",
                title="Daria Pancho",
                description="Seed account",
                url="https://www.youtube.com/@dariapancho",
            )
        ],
        max_youtube_search_calls=3,
        max_candidates_per_platform=20,
    )

    assert [(candidate.platform, candidate.external_id) for candidate in outcome.candidates] == [
        (Platform.YOUTUBE, "yt-1"),
        (Platform.INSTAGRAM, "ig-1"),
    ]
    assert outcome.notes == [
        "YouTube: FOUND (1)",
        "Instagram: FOUND (1)",
        "TikTok: ERROR — provider timeout",
    ]


def test_discover_competitors_for_onboarding_drops_noncollectible_instagram_and_tiktok_candidates(monkeypatch):
    monkeypatch.setattr(platform_onboarding, "_discover_youtube_search_candidates", lambda **kwargs: [])
    monkeypatch.setattr(
        platform_onboarding,
        "_search_instagram_candidates_raw",
        lambda **kwargs: [
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.INSTAGRAM,
                external_id="ig-good",
                handle="reelhub",
                url="https://www.instagram.com/reelhub/",
                display_name="Reel Hub",
                description="english teacher reels",
                query_hits={"english teachers"},
            ),
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.INSTAGRAM,
                external_id="ig-bad",
                handle="feedonly",
                url="https://www.instagram.com/feedonly/",
                display_name="Feed Only",
                description="feed videos only",
                query_hits={"english teachers"},
            ),
        ],
    )
    monkeypatch.setattr(
        platform_onboarding,
        "_search_tiktok_candidates_raw",
        lambda **kwargs: [
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.TIKTOK,
                external_id="tt-good",
                handle="teachertok",
                url="https://www.tiktok.com/@teachertok",
                display_name="TeacherTok",
                description="english short lessons",
                query_hits={"english teachers"},
            ),
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.TIKTOK,
                external_id="tt-bad",
                handle="emptytok",
                url="https://www.tiktok.com/@emptytok",
                display_name="EmptyTok",
                description="no usable videos",
                query_hits={"english teachers"},
            ),
        ],
    )

    def fake_fetch_instagram_profiles_cached(*, inputs, context=None):
        lookup = inputs[0]
        if "reelhub" in lookup:
            return [
                {
                    "id": "ig-good",
                    "username": "reelhub",
                    "latestPosts": [
                        {
                            "id": "reel-1",
                            "productType": "clips",
                            "url": "https://www.instagram.com/reel/reel-1/",
                            "caption": "English teacher reel",
                            "timestamp": "2024-07-03T10:30:00.000Z",
                            "videoViewCount": 2400,
                        }
                    ],
                }
            ]
        return [
            {
                "id": "ig-bad",
                "username": "feedonly",
                "latestPosts": [
                    {
                        "id": "feed-1",
                        "productType": "feed",
                        "url": "https://www.instagram.com/p/feed-1/",
                        "caption": "Feed video",
                        "timestamp": "2024-07-03T10:30:00.000Z",
                        "videoViewCount": 1900,
                    }
                ],
            }
        ]

    def fake_fetch_tiktok_profile_feed_cached(*, handle, results_per_page, context=None):
        if handle == "teachertok":
            return [
                {
                    "id": "vid-1",
                    "text": "english teacher short lesson",
                    "createTimeISO": "2024-04-03T14:22:40.000Z",
                    "authorMeta": {"id": "auth-1", "name": "teachertok", "nickName": "TeacherTok"},
                    "webVideoUrl": "https://www.tiktok.com/@teachertok/video/vid-1",
                    "videoMeta": {"duration": 19},
                    "playCount": 8800,
                    "diggCount": 200,
                    "commentCount": 11,
                    "shareCount": 4,
                }
            ]
        return []

    monkeypatch.setattr(platform_onboarding, "fetch_instagram_profiles_cached", fake_fetch_instagram_profiles_cached)
    monkeypatch.setattr(platform_onboarding, "fetch_tiktok_profile_feed_cached", fake_fetch_tiktok_profile_feed_cached)

    outcome = platform_onboarding.discover_competitors_for_onboarding(
        keywords=["english teachers"],
        seed=_seed(
            platform=Platform.INSTAGRAM,
            external_id="ig-seed",
            handle="creator",
            title="Creator",
            description="English teacher",
            url="https://www.instagram.com/creator/",
        ),
        competitors=[],
        linked_accounts=[],
        max_youtube_search_calls=1,
        max_candidates_per_platform=20,
    )

    assert [(candidate.platform, candidate.external_id) for candidate in outcome.candidates] == [
        (Platform.INSTAGRAM, "ig-good"),
        (Platform.TIKTOK, "tt-good"),
    ]
    assert outcome.notes == [
        "YouTube: EMPTY — по текущим поисковым фразам поиск был выполнен, но кандидаты не найдены.",
        "Instagram: FOUND (1)",
        "TikTok: FOUND (1)",
    ]


def test_discover_competitors_for_onboarding_rejects_offtopic_education_channels_for_english_niche(monkeypatch):
    monkeypatch.setattr(
        platform_onboarding,
        "_discover_youtube_search_candidates",
        lambda **kwargs: [
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.YOUTUBE,
                external_id="yt-english",
                handle="englishnotes",
                url="https://www.youtube.com/@englishnotes",
                display_name="English Notes",
                description="lesson plans for english teachers",
                query_hits={"english teachers", "lesson plans"},
                metadata={"rank_hint": 5000},
            ),
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.YOUTUBE,
                external_id="yt-history",
                handle="historyege",
                url="https://www.youtube.com/@historyege",
                display_name="History EGE",
                description="егэ по истории и обществознанию",
                query_hits={"english teachers"},
                metadata={"rank_hint": 9000},
            ),
        ],
    )
    monkeypatch.setattr(platform_onboarding, "_search_instagram_candidates_raw", lambda **kwargs: [])
    monkeypatch.setattr(platform_onboarding, "_search_tiktok_candidates_raw", lambda **kwargs: [])

    calls = {"youtube": 0}

    def fake_recent_youtube_short_texts(*, candidate, n, context=None):
        calls["youtube"] += 1
        if candidate.external_id == "yt-english":
            return ["english teacher lesson plans", "worksheet ideas for english tutors"]
        return ["разбор егэ по истории", "история россии для егэ"]

    monkeypatch.setattr(platform_onboarding, "_fetch_recent_youtube_short_texts", fake_recent_youtube_short_texts)

    outcome = platform_onboarding.discover_competitors_for_onboarding(
        keywords=["english teachers", "lesson plans"],
        seed=_seed(
            platform=Platform.INSTAGRAM,
            external_id="ig-seed",
            handle="creator",
            title="Creator",
            description="English teacher",
            url="https://www.instagram.com/creator/",
        ),
        competitors=[],
        linked_accounts=[],
        max_youtube_search_calls=1,
        max_candidates_per_platform=20,
    )

    assert [(candidate.platform, candidate.external_id) for candidate in outcome.candidates] == [
        (Platform.YOUTUBE, "yt-english"),
    ]
    assert calls == {"youtube": 1}


def test_search_queries_prioritizes_theme_specific_phrases_over_generic_tutor_terms():
    queries = platform_onboarding._search_queries(
        [
            "онлайн репетитор",
            "онлайн школу по английскому",
            "группы для преподавателей",
            "онлайн репетитор по английскому",
        ],
        max_queries=2,
    )

    assert queries[0] == "группы для преподавателей"
    assert "онлайн репетитор" not in queries
    assert set(queries) == {
        "группы для преподавателей",
        "онлайн репетитор по английскому",
    }


def test_candidate_survives_only_if_recent_short_form_content_matches_niche(monkeypatch):
    candidate = platform_onboarding._DiscoveryCandidate(
        platform=Platform.YOUTUBE,
        external_id="yt-1",
        handle="teacherhub",
        url="https://www.youtube.com/@teacherhub",
        display_name="Teacher Hub",
        description="lesson planning for english teachers",
        query_hits={"english teachers", "lesson plans"},
        metadata={"rank_hint": 1000},
    )

    def fake_recent_youtube_short_texts(*, candidate, n, context=None):
        return ["history exam tips", "егэ по истории"]

    monkeypatch.setattr(platform_onboarding, "_fetch_recent_youtube_short_texts", fake_recent_youtube_short_texts)

    validated, reason = platform_onboarding._collector_aware_candidates(
        platform=Platform.YOUTUBE,
        candidates=[candidate],
        keywords=["english teachers", "lesson plans"],
        max_candidates=20,
        context=None,
    )

    assert validated == []
    assert "recent Shorts по теме" in reason


def test_retry_cache_reuses_youtube_collectible_probe_across_setup_retries(monkeypatch):
    clear_retry_cache()
    calls = {"youtube": 0}

    class FakeClient:
        def channels_list(self, *, part, ids=None, for_handle=None):
            assert ids == ["yt-1"]
            return [{"id": "yt-1", "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]

        def playlist_items(self, *, playlist_id, max_results):
            return [
                {"contentDetails": {"videoId": "short-1"}},
                {"contentDetails": {"videoId": "short-2"}},
            ]

        def videos_list(self, *, ids, part):
            calls["youtube"] += 1
            return [
                {
                    "id": "short-1",
                    "snippet": {"title": "English lesson plan", "publishedAt": "2026-03-20T12:00:00Z"},
                    "contentDetails": {"duration": "PT45S"},
                },
                {
                    "id": "short-2",
                    "snippet": {"title": "Teacher worksheet ideas", "publishedAt": "2026-03-20T12:05:00Z"},
                    "contentDetails": {"duration": "PT52S"},
                },
            ]

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "get_youtube_client", lambda: FakeClient())
    candidate = platform_onboarding._DiscoveryCandidate(
        platform=Platform.YOUTUBE,
        external_id="yt-1",
        handle="teacherhub",
        url="https://www.youtube.com/@teacherhub",
        display_name="Teacher Hub",
        description="lesson planning for english teachers",
        query_hits={"english teachers"},
    )

    first = platform_onboarding._fetch_recent_youtube_short_texts(candidate=candidate, n=3, context=None)
    second = platform_onboarding._fetch_recent_youtube_short_texts(candidate=candidate, n=3, context=None)

    assert first == ["English lesson plan", "Teacher worksheet ideas"]
    assert second == first
    assert calls == {"youtube": 1}


def test_discover_competitors_for_onboarding_preserves_manual_competitors_as_query_and_ranking_hints(monkeypatch):
    seen_competitors: list[SeedResolution] = []

    def fake_youtube_search(**kwargs):
        seen_competitors.extend(kwargs["competitors"])
        return [
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.YOUTUBE,
                external_id="yt-1",
                handle="teacherhub",
                url="https://www.youtube.com/@teacherhub",
                display_name="Teacher Hub",
                description="lesson planning for english teachers",
                query_hits={"teacher groups"},
                metadata={"rank_hint": 5000},
            ),
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.YOUTUBE,
                external_id="yt-2",
                handle="genericchannel",
                url="https://www.youtube.com/@genericchannel",
                display_name="Generic Channel",
                description="broad education videos",
                query_hits={"teacher groups"},
                metadata={"rank_hint": 5000},
            ),
        ]

    monkeypatch.setattr(platform_onboarding, "_discover_youtube_search_candidates", fake_youtube_search)
    monkeypatch.setattr(platform_onboarding, "_search_instagram_candidates_raw", lambda **kwargs: [])
    monkeypatch.setattr(platform_onboarding, "_search_tiktok_candidates_raw", lambda **kwargs: [])
    monkeypatch.setattr(platform_onboarding, "_collector_aware_candidates", lambda **kwargs: (kwargs["candidates"], ""))

    competitors = [
        _seed(
            platform=Platform.YOUTUBE,
            external_id="manual-1",
            handle="teachergroups",
            title="Teacher Groups",
            description="lesson planning for english teachers",
            url="https://www.youtube.com/@teachergroups",
        )
    ]

    outcome = platform_onboarding.discover_competitors_for_onboarding(
        keywords=["teacher groups"],
        seed=_seed(
            platform=Platform.INSTAGRAM,
            external_id="ig-seed",
            handle="creator",
            title="Creator",
            description="English teacher",
            url="https://www.instagram.com/creator/",
        ),
        competitors=competitors,
        linked_accounts=[],
        max_youtube_search_calls=3,
        max_candidates_per_platform=20,
    )

    assert [item.external_id for item in seen_competitors] == ["manual-1"]
    assert [(candidate.platform, candidate.external_id) for candidate in outcome.candidates] == [
        (Platform.YOUTUBE, "yt-1"),
        (Platform.YOUTUBE, "yt-2"),
    ]
    assert outcome.candidates[0].external_id == "yt-1"


def test_discover_competitors_for_onboarding_respects_zero_youtube_search_calls(monkeypatch):
    called = {"youtube": 0}

    def fake_youtube_search(**kwargs):
        called["youtube"] += 1
        return []

    monkeypatch.setattr(platform_onboarding, "_discover_youtube_search_candidates", fake_youtube_search)
    monkeypatch.setattr(platform_onboarding, "_search_instagram_candidates_raw", lambda **kwargs: [])
    monkeypatch.setattr(platform_onboarding, "_search_tiktok_candidates_raw", lambda **kwargs: [])

    outcome = platform_onboarding.discover_competitors_for_onboarding(
        keywords=["english tutors"],
        seed=_seed(
            platform=Platform.INSTAGRAM,
            external_id="ig-seed",
            handle="creator",
            title="Creator",
            description="English teacher",
            url="https://www.instagram.com/creator/",
        ),
        competitors=[],
        linked_accounts=[],
        max_youtube_search_calls=0,
        max_candidates_per_platform=20,
        context=SetupRunContext(),
    )

    assert called == {"youtube": 0}
    assert outcome.notes[0] == "YouTube: SKIPPED — YT_MAX_SEARCH_CALLS_PER_SETUP=0; поиск YouTube отключен для этого setup."


def test_setup_context_reuses_instagram_profile_between_seed_resolve_and_recent_content(monkeypatch):
    clear_retry_cache()
    calls = {"profiles": 0}
    context = SetupRunContext()

    class FakeClient:
        def fetch_profiles(self, *, inputs):
            calls["profiles"] += 1
            assert inputs == ["https://www.instagram.com/nasa/"]
            return [
                {
                    "id": "ig-1",
                    "username": "nasa",
                    "url": "https://www.instagram.com/nasa/",
                    "latestPosts": [
                        {
                            "id": "post-1",
                            "productType": "clips",
                            "url": "https://www.instagram.com/reel/post-1/",
                            "caption": "Mars update",
                            "timestamp": "2024-07-03T10:30:00.000Z",
                            "videoViewCount": 1500,
                        }
                    ],
                }
            ]

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_get_instagram_client", lambda: FakeClient())

    seed = seed_resolver.resolve_seed_for_platform(
        platform=Platform.INSTAGRAM,
        raw_input="https://www.instagram.com/nasa/",
        context=context,
    )
    texts = platform_onboarding.get_recent_seed_content_texts(seed=seed, n=5, context=context)

    assert texts == ["Mars update"]
    assert calls == {"profiles": 1}


def test_instagram_403_marks_platform_unavailable_for_rest_of_setup(monkeypatch):
    calls = {"search": 0}
    context = SetupRunContext()

    class FakeClient:
        def search_profiles(self, *, query, limit=None):
            calls["search"] += 1
            raise platform_onboarding.InstagramApiError(
                "Apify Instagram search API error: status=403 body={'error': {'type': 'platform-feature-disabled'}}"
            )

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_get_instagram_client", lambda: FakeClient())

    with pytest.raises(platform_onboarding.InstagramApiError, match="status=403"):
        platform_onboarding._cached_instagram_search_results(
            query="english tutors",
            limit=5,
            context=context,
        )

    state = get_platform_state(context, Platform.INSTAGRAM)
    assert state.state == PLATFORM_STATE_UNAVAILABLE

    with pytest.raises(platform_onboarding.PlatformOnboardingError, match="status=403"):
        platform_onboarding._cached_instagram_search_results(
            query="english tutors",
            limit=5,
            context=context,
        )

    assert calls == {"search": 1}


def test_setup_context_reuses_instagram_search_query_results(monkeypatch):
    calls = {"search": 0}
    context = SetupRunContext()

    class FakeClient:
        def search_profiles(self, *, query, limit=None):
            calls["search"] += 1
            return [
                {"id": "ig-1", "username": "teacher_hub", "full_name": "Teacher Hub"},
                {"id": "ig-2", "username": "lessonlab", "full_name": "Lesson Lab"},
            ]

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_get_instagram_client", lambda: FakeClient())

    first = platform_onboarding._cached_instagram_search_results(query="english tutors", limit=5, context=context)
    second = platform_onboarding._cached_instagram_search_results(query="english tutors", limit=5, context=context)

    assert [item["id"] for item in first] == ["ig-1", "ig-2"]
    assert [item["id"] for item in second] == ["ig-1", "ig-2"]
    assert calls == {"search": 1}


def test_retry_cache_reuses_instagram_search_results_across_setup_retries(monkeypatch):
    clear_retry_cache()
    calls = {"search": 0}

    class FakeClient:
        def search_profiles(self, *, query, limit=None):
            calls["search"] += 1
            return [
                {"id": "ig-1", "username": "teacher_hub", "full_name": "Teacher Hub"},
                {"id": "ig-2", "username": "lessonlab", "full_name": "Lesson Lab"},
            ]

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_get_instagram_client", lambda: FakeClient())

    first = platform_onboarding._cached_instagram_search_results(query="english tutors", limit=4, context=None)
    second = platform_onboarding._cached_instagram_search_results(query="english tutors", limit=4, context=None)

    assert [item["id"] for item in first] == ["ig-1", "ig-2"]
    assert [item["id"] for item in second] == ["ig-1", "ig-2"]
    assert calls == {"search": 1}
