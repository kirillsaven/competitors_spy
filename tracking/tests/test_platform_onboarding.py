from __future__ import annotations

import pytest

from tracking.adapters.base import SeedResolution
from tracking.models import Platform
from tracking.services import platform_onboarding
from tracking.services import seed_resolver
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


def test_get_recent_seed_content_texts_reads_instagram_captions_without_view_filter(monkeypatch):
    sample_profiles = [
        {
            "id": "ig-1",
            "username": "nasa",
            "url": "https://www.instagram.com/nasa/",
            "latestPosts": [
                {
                    "id": "post-1",
                    "caption": "Mars update",
                    "timestamp": "2024-07-03T10:30:00.000Z",
                },
                {
                    "id": "post-2",
                    "description": "Moon update",
                    "timestamp": "2024-07-03T10:35:00.000Z",
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

    assert texts == ["Mars update", "Moon update"]


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

    assert search_calls == ["english teachers", "teacher groups"]
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

    assert search_calls == ["english teachers", "teacher groups"]
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


def test_setup_context_reuses_instagram_profile_between_seed_resolve_and_recent_content(monkeypatch):
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
                    "latestPosts": [{"id": "post-1", "caption": "Mars update"}],
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
