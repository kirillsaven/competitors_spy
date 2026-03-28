from __future__ import annotations

from datetime import UTC, datetime, timedelta

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

        def fetch_profiles(self, *, inputs):
            return []

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_get_instagram_client", lambda: FakeClient())
    monkeypatch.setattr(platform_onboarding, "_expand_instagram_related_candidates", lambda **kwargs: kwargs["candidates"])

    candidates = platform_onboarding.discover_instagram_competitors(
        keywords=["english teachers", "teacher groups"],
        max_candidates=10,
    )

    assert search_calls[0] == "english teachers"
    assert "teacher groups" in search_calls or "english teacher groups" in search_calls
    assert "english tutor" in search_calls
    assert [candidate.external_id for candidate in candidates] == ["ig-1", "ig-3", "ig-2"]
    assert candidates[0].reason.startswith("search: ")
    assert "english teachers" in candidates[0].reason


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

    assert search_calls[0] == "english teachers"
    assert "teacher groups" in search_calls or "english teacher groups" in search_calls
    assert "english tutor" in search_calls
    assert [candidate.external_id for candidate in candidates] == ["tt-1", "tt-3", "tt-2"]
    assert candidates[0].reason.startswith("search: ")
    assert "english teachers" in candidates[0].reason


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


def test_discover_youtube_search_candidates_respects_budget_without_nameerror(monkeypatch):
    queries_seen: list[str] = []

    monkeypatch.setattr(
        platform_onboarding,
        "_discovery_queries",
        lambda **kwargs: ["english teachers", "teacher groups", "lesson plans"],
    )

    def fake_cached_youtube_search_channel_ids(*, query, max_results, context=None):
        queries_seen.append(query)
        return {
            "english teachers": ["yt-1", "yt-2"],
            "teacher groups": ["yt-2", "yt-3"],
            "lesson plans": ["yt-4"],
        }[query]

    class FakeClient:
        def channels_list(self, *, part, ids):
            return [
                {
                    "id": channel_id,
                    "snippet": {"title": f"Channel {channel_id}", "description": "English teaching shorts"},
                    "statistics": {"subscriberCount": "1000"},
                }
                for channel_id in ids
            ]

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_cached_youtube_search_channel_ids", fake_cached_youtube_search_channel_ids)
    monkeypatch.setattr(platform_onboarding, "get_youtube_client", lambda: FakeClient())

    candidates = platform_onboarding._discover_youtube_search_candidates(
        keywords=["english teachers", "teacher groups"],
        competitors=[],
        max_search_calls=3,
    )

    assert queries_seen == ["english teachers", "teacher groups", "lesson plans"]
    assert [candidate.external_id for candidate in candidates] == ["yt-1", "yt-2", "yt-3", "yt-4"]


def test_discover_youtube_search_candidates_uses_extra_queries_when_recall_is_low(monkeypatch):
    queries_seen: list[str] = []

    monkeypatch.setattr(
        platform_onboarding,
        "_discovery_queries",
        lambda **kwargs: [
            "english teachers",
            "teacher groups",
            "lesson plans",
            "online english school",
            "english tutor",
        ],
    )

    def fake_cached_youtube_search_channel_ids(*, query, max_results, context=None):
        queries_seen.append(query)
        return {
            "english teachers": ["yt-1"],
            "teacher groups": ["yt-2"],
            "lesson plans": ["yt-3"],
            "online english school": ["yt-4"],
            "english tutor": ["yt-5"],
        }[query]

    class FakeClient:
        def channels_list(self, *, part, ids):
            return [
                {
                    "id": channel_id,
                    "snippet": {"title": f"Channel {channel_id}", "description": "English teaching shorts"},
                    "statistics": {"subscriberCount": "1000"},
                }
                for channel_id in ids
            ]

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_cached_youtube_search_channel_ids", fake_cached_youtube_search_channel_ids)
    monkeypatch.setattr(platform_onboarding, "get_youtube_client", lambda: FakeClient())

    candidates = platform_onboarding._discover_youtube_search_candidates(
        keywords=["english teachers", "teacher groups"],
        competitors=[],
        max_search_calls=2,
    )

    assert queries_seen == [
        "english teachers",
        "teacher groups",
        "lesson plans",
        "online english school",
        "english tutor",
    ]
    assert [candidate.external_id for candidate in candidates] == ["yt-1", "yt-2", "yt-3", "yt-4", "yt-5"]


def test_collector_aware_instagram_filters_mixed_topic_accounts(monkeypatch):
    keywords = [
        "английский язык",
        "уроки английского",
        "преподаватели английского",
        "английский для взрослых",
    ]
    relevant = platform_onboarding._DiscoveryCandidate(
        platform=Platform.INSTAGRAM,
        external_id="ig-good",
        handle="english.good",
        url="https://www.instagram.com/english.good/",
        display_name="Английский для взрослых",
        description="Репетитор по английскому онлайн",
        query_hits={"английский для взрослых"},
    )
    mixed = platform_onboarding._DiscoveryCandidate(
        platform=Platform.INSTAGRAM,
        external_id="ig-mixed",
        handle="english.mixed",
        url="https://www.instagram.com/english.mixed/",
        display_name="Английский онлайн | для взрослых",
        description="Препод по английскому",
        query_hits={"английский для взрослых"},
    )

    monkeypatch.setattr(
        platform_onboarding,
        "_batch_fetch_recent_instagram_reel_texts",
        lambda **kwargs: {
            "ig-good": (
                [
                    "Разбираем ошибку на уроке английского и учим полезную фразу.",
                    "Как взрослому начать говорить на английском без зубрежки.",
                    "Мини-урок английского: 3 фразы для small talk.",
                ],
                [1500, 1200, 900],
                6,
            ),
            "ig-mixed": (
                [
                    "Новый эпизод подкаста об отношениях уже в профиле.",
                    "Почему в долгих отношениях пропадает близость.",
                    "Мой личный влог про семью и поддержку.",
                    "Я препод по английскому, запись на занятия в шапке профиля.",
                    "Еще один выпуск подкаста про отношения.",
                ],
                [2200, 1800, 900, 4000, 1700],
                8,
            ),
        },
    )

    validated, reason = platform_onboarding._collector_aware_candidates(
        platform=Platform.INSTAGRAM,
        candidates=[relevant, mixed],
        keywords=keywords,
        max_candidates=10,
    )

    assert reason == ""
    assert [candidate.external_id for candidate in validated] == ["ig-good"]
    assert mixed.metadata["content_theme_strong_text_matches"] == 1


def test_collector_aware_instagram_keeps_exam_accounts_with_consistent_recent_reels(monkeypatch):
    keywords = [
        "английский язык",
        "уроки английского",
        "IELTS",
        "TOEFL",
    ]
    candidate = platform_onboarding._DiscoveryCandidate(
        platform=Platform.INSTAGRAM,
        external_id="ig-exam",
        handle="exam.english",
        url="https://www.instagram.com/exam.english/",
        display_name="Английский для IELTS и TOEFL",
        description="Преподаватель английского и IELTS",
        query_hits={"ielts english"},
    )

    monkeypatch.setattr(
        platform_onboarding,
        "_batch_fetch_recent_instagram_reel_texts",
        lambda **kwargs: {
            "ig-exam": (
                [
                    "Как сдать IELTS на 7.0: 3 ошибки в speaking.",
                    "TOEFL writing: шаблон эссе и полезные фразы.",
                    "Разбор эссе IELTS и типовых ошибок на экзамене.",
                ],
                [5000, 4200, 3800],
                5,
            ),
        },
    )

    validated, reason = platform_onboarding._collector_aware_candidates(
        platform=Platform.INSTAGRAM,
        candidates=[candidate],
        keywords=keywords,
        max_candidates=10,
    )

    assert reason == ""
    assert [item.external_id for item in validated] == ["ig-exam"]


def test_collector_aware_instagram_recovers_compound_hashtag_science_accounts(monkeypatch):
    keywords = [
        "engineering projects",
        "science experiments",
        "DIY projects",
        "robotics",
    ]
    candidate = platform_onboarding._DiscoveryCandidate(
        platform=Platform.INSTAGRAM,
        external_id="ig-science",
        handle="thedadlab",
        url="https://www.instagram.com/thedadlab/",
        display_name="Science Experiments for Kids",
        description="DIY science projects and experiments",
        query_hits={"science experiments"},
        metadata={"verified": True},
    )

    monkeypatch.setattr(
        platform_onboarding,
        "_batch_fetch_recent_instagram_reel_texts",
        lambda **kwargs: {
            "ig-science": (
                [
                    "No teacups were harmed in the making of this video #scienceexperiments #scienceisfun",
                    "Wait, why does paper open itself when it gets wet? A wow experiment for kids.",
                    "Save this DIY project for later.",
                ],
                [12000, 9000, 7500],
                5,
            ),
        },
    )

    validated, reason = platform_onboarding._collector_aware_candidates(
        platform=Platform.INSTAGRAM,
        candidates=[candidate],
        keywords=keywords,
        max_candidates=10,
    )

    assert reason == ""
    assert [item.external_id for item in validated] == ["ig-science"]


def test_collector_aware_instagram_filters_other_language_accounts_for_english_niche(monkeypatch):
    keywords = [
        "английский язык",
        "уроки английского",
        "преподаватели английского",
        "планы уроков",
    ]
    candidate = platform_onboarding._DiscoveryCandidate(
        platform=Platform.INSTAGRAM,
        external_id="ig-de",
        handle="deutsch_by_schule",
        url="https://www.instagram.com/deutsch_by_schule/",
        display_name="Планы-конспекты по немецкому",
        description="Учитель немецкого языка",
        query_hits={"планы уроков"},
    )

    monkeypatch.setattr(
        platform_onboarding,
        "_batch_fetch_recent_instagram_reel_texts",
        lambda **kwargs: {
            "ig-de": (
                [
                    "Планы-конспекты по немецкому языку для учителей.",
                    "Урок немецкого языка: материалы и рабочие листы.",
                    "deutschunterricht #немецкийязык #учительнемецкого",
                ],
                [500, 450, 420],
                5,
            ),
        },
    )

    validated, reason = platform_onboarding._collector_aware_candidates(
        platform=Platform.INSTAGRAM,
        candidates=[candidate],
        keywords=keywords,
        max_candidates=20,
    )

    assert validated == []
    assert "по теме" in reason


def test_theme_profile_passes_requires_english_affinity_for_english_niche():
    candidate = platform_onboarding._DiscoveryCandidate(
        platform=Platform.INSTAGRAM,
        external_id="ig-math",
        handle="olga.math_repetitor",
        url="https://www.instagram.com/olga.math_repetitor/",
        display_name="Репетитор по математике",
        description="Интерактивы и планы уроков",
        query_hits={"планы уроков"},
    )

    assert not platform_onboarding._theme_profile_passes(
        candidate=candidate,
        keywords=["английский язык", "планы уроков", "преподаватели английского"],
    )


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
        "YouTube: FOUND (1) — найдено 1 валидных кандидатов; после всех поисковых фраз и проверок больше подтвержденных профилей не осталось.",
        "Instagram: FOUND (1) — найдено 1 валидных кандидатов; после всех поисковых фраз и проверок больше подтвержденных профилей не осталось.",
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

    def fake_fetch_instagram_profiles_cached(*, inputs, context=None, purpose=None, context_id=None):
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
                            "timestamp": "2026-03-03T10:30:00.000Z",
                            "videoViewCount": 2400,
                        },
                        {
                            "id": "reel-2",
                            "productType": "clips",
                            "url": "https://www.instagram.com/reel/reel-2/",
                            "caption": "Lesson planning for english teachers",
                            "timestamp": "2026-03-18T10:30:00.000Z",
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

    def fake_fetch_tiktok_profile_feeds_cached(*, handles, results_per_page, context=None, purpose=None, context_id=None):
        out = {}
        for handle in handles:
            if handle == "teachertok":
                out[handle] = [
                    {
                        "id": "vid-1",
                        "text": "english teacher short lesson",
                        "createTimeISO": "2026-03-03T14:22:40.000Z",
                        "authorMeta": {"id": "auth-1", "name": "teachertok", "nickName": "TeacherTok"},
                        "webVideoUrl": "https://www.tiktok.com/@teachertok/video/vid-1",
                        "videoMeta": {"duration": 19},
                        "playCount": 8800,
                        "diggCount": 200,
                        "commentCount": 11,
                        "shareCount": 4,
                    },
                    {
                        "id": "vid-2",
                        "text": "lesson planning ideas for english tutors",
                        "createTimeISO": "2026-03-18T14:22:40.000Z",
                        "authorMeta": {"id": "auth-1", "name": "teachertok", "nickName": "TeacherTok"},
                        "webVideoUrl": "https://www.tiktok.com/@teachertok/video/vid-2",
                        "videoMeta": {"duration": 22},
                        "playCount": 7600,
                        "diggCount": 170,
                        "commentCount": 9,
                        "shareCount": 3,
                    },
                ]
            else:
                out[handle] = []
        return out

    monkeypatch.setattr(platform_onboarding, "fetch_instagram_profiles_cached", fake_fetch_instagram_profiles_cached)
    monkeypatch.setattr(platform_onboarding, "fetch_tiktok_profile_feeds_cached", fake_fetch_tiktok_profile_feeds_cached)

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
        "Instagram: FOUND (1) — найдено 1 валидных кандидатов; после всех поисковых фраз и проверок больше подтвержденных профилей не осталось.",
        "TikTok: FOUND (1) — найдено 1 валидных кандидатов; после всех поисковых фраз и проверок больше подтвержденных профилей не осталось.",
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

    def fake_recent_youtube_short_signals(*, candidate, n, context=None):
        calls["youtube"] += 1
        if candidate.external_id == "yt-english":
            return ["english teacher lesson plans", "worksheet ideas for english tutors"], [12000, 9000], 3
        return ["разбор егэ по истории", "история россии для егэ"], [15000, 11000], 3

    monkeypatch.setattr(platform_onboarding, "_fetch_recent_youtube_short_signals", fake_recent_youtube_short_signals)

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
    assert calls == {"youtube": 2}


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

    assert any("английскому" in query for query in queries)
    assert len(queries) == 2
    assert queries[0] != "онлайн репетитор"


def test_search_queries_skip_identity_like_phrases_when_theme_queries_exist():
    queries = platform_onboarding._search_queries(
        [
            "репетитора",
            "английский",
            "преподавателей",
            "группы",
            "урока",
            "дарья панчо",
        ],
        max_queries=4,
    )

    assert "дарья панчо" not in queries
    assert any("англий" in query for query in queries)


def test_search_queries_drop_overlong_broken_phrases():
    queries = platform_onboarding.build_search_ready_keywords(
        keywords=[
            "репетитор",
            "английский",
            "английский учеба английскийонлайн репетитор",
            "уроки английский учеба английскийонлайн",
            "учеников занимаются с репетитором благодаря",
            "свою онлайн школу по английскому",
        ],
        max_keywords=6,
    )

    assert all(len(query.split()) <= 4 for query in queries)
    assert all(len(query) <= 48 for query in queries)
    assert len(queries) >= 6
    assert "преподаватель английского" in queries
    assert "уроки английского" in queries
    assert any("школа английского" in query for query in queries)
    assert all("английскийонлайн" not in query for query in queries)


def test_search_queries_drop_identity_and_marketing_phrases_for_youtube_teacher_seed():
    queries = platform_onboarding.build_search_ready_keywords(
        keywords=[
            "английского языка",
            "получайте дополнительные полезные материалы",
            "учите английский язык с нами",
            "преподаватель английского языка",
            "меня зовут александр бебрис",
        ],
        max_keywords=8,
    )

    assert "меня зовут александр бебрис" not in queries
    assert "получайте дополнительные полезные материалы" not in queries
    assert all("александр" not in query for query in queries)
    assert any("репетитор английского языка" in query or "преподаватель английского языка" in query for query in queries)
    assert any("англий" in query for query in queries)


def test_search_queries_demote_self_referential_queries_for_teacher_seed():
    queries = platform_onboarding.build_search_ready_keywords(
        keywords=[
            "открываю свою онлайн школу",
            "помогаю ученикам заговорить на английском",
            "репетитор английского",
            "уроки английского",
            "преподаватель английского",
        ],
        max_keywords=4,
    )

    assert "открываю свою онлайн школу" not in queries
    assert "помогаю ученикам заговорить на английском" not in queries
    assert "уроки английского" in queries
    assert any("англий" in query and ("преподав" in query or "репетитор" in query) for query in queries)


def test_search_queries_keep_lesson_intent_in_top_youtube_budget():
    queries = platform_onboarding.build_search_ready_keywords(
        keywords=[
            "преподаватель английского",
            "онлайн репетитор по английскому",
            "группы преподавателей английского",
            "уроки английского",
            "школа английского",
            "репетитор английского",
        ],
        max_keywords=3,
    )

    assert len(queries) == 3
    assert "уроки английского" in queries
    assert any("преподав" in query or "репетитор" in query for query in queries)


def test_search_queries_do_not_generate_broken_variants_from_ready_multiword_phrases():
    queries = platform_onboarding.build_search_ready_keywords(
        keywords=[
            "английский",
            "английский для начинающих",
            "английский для взрослых",
            "разговорный английский",
            "онлайн репетитор по английскому",
        ],
        max_keywords=8,
    )

    assert "английский для начинающих" in queries
    assert "английский для взрослых" in queries
    assert "разговорный английский" in queries
    assert "преподаватель английский для" not in queries
    assert "репетитор английский для" not in queries


def test_search_queries_compact_long_sentence_like_keywords_into_searchable_phrases():
    queries = platform_onboarding.build_search_ready_keywords(
        keywords=[
            "evidence based strategies and tools",
            "explore evidence based strategies",
            "doctor turned entrepreneur",
            "psychology of making money",
            "comment books and I'll send you the list",
        ],
        max_keywords=6,
    )

    assert any("evidence based" in query for query in queries)
    assert any("doctor turned entrepreneur" == query or "turned entrepreneur" in query for query in queries)
    assert any("making money" in query for query in queries)
    assert all("comment books" not in query for query in queries)


def test_youtube_fallback_queries_add_cross_language_english_intents():
    queries = platform_onboarding._youtube_fallback_queries(
        [
            "английский для начинающих",
            "уроки английского",
            "онлайн школа английского",
            "репетитор английского",
        ]
    )

    assert "english for beginners" in queries
    assert "english lessons" in queries
    assert "english teacher" in queries
    assert "online english school" in queries


def test_youtube_fallback_queries_add_dota_guide_intents():
    queries = platform_onboarding._youtube_fallback_queries(
        [
            "dota 2",
            "гайды dota 2",
            "патч dota 2",
        ]
    )

    assert "dota 2 guide" in queries
    assert "dota 2 tips" in queries
    assert "dota 2 patch" in queries


def test_dota_and_dota2_aliases_match_in_theme_metrics():
    metrics = platform_onboarding._theme_agreement_metrics(
        texts=["Top DOTA2 rampages #dota2"],
        keywords=["dota"],
    )

    assert metrics["anchor_overlap"] >= 1
    assert metrics["specific_overlap"] >= 1


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

    def fake_recent_youtube_short_signals(*, candidate, n, context=None):
        return ["history exam tips", "егэ по истории"], [5000, 4200], 3

    monkeypatch.setattr(platform_onboarding, "_fetch_recent_youtube_short_signals", fake_recent_youtube_short_signals)

    validated, reason = platform_onboarding._collector_aware_candidates(
        platform=Platform.YOUTUBE,
        candidates=[candidate],
        keywords=["english teachers", "lesson plans"],
        max_candidates=20,
        context=None,
    )

    assert validated == []
    assert "recent Shorts по теме" in reason


def test_candidate_is_dropped_if_it_has_less_than_two_recent_shorts(monkeypatch):
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

    def fake_recent_youtube_short_signals(*, candidate, n, context=None):
        return ["english teacher lesson plans"], [5000], 1

    monkeypatch.setattr(platform_onboarding, "_fetch_recent_youtube_short_signals", fake_recent_youtube_short_signals)

    validated, reason = platform_onboarding._collector_aware_candidates(
        platform=Platform.YOUTUBE,
        candidates=[candidate],
        keywords=["english teachers", "lesson plans"],
        max_candidates=20,
        context=None,
    )

    assert validated == []
    assert "меньше 2 recent Shorts" in reason


def test_candidate_with_english_profile_and_instructional_recent_shorts_survives(monkeypatch):
    candidate = platform_onboarding._DiscoveryCandidate(
        platform=Platform.YOUTUBE,
        external_id="yt-english-club",
        handle="english-club",
        url="https://www.youtube.com/@english-club",
        display_name="English Club. Английский для начинающих и знатоков",
        description="Английский для начинающих, фразы и грамматика",
        query_hits={"уроки английского", "английский для начинающих"},
        metadata={"rank_hint": 8000},
    )

    def fake_recent_youtube_short_signals(*, candidate, n, context=None):
        return [
            "Предлоги IN AT ON",
            "Фразы на каждый день",
            "Запоминаем выражения с by",
        ], [14000, 9000, 8500], 3

    monkeypatch.setattr(platform_onboarding, "_fetch_recent_youtube_short_signals", fake_recent_youtube_short_signals)

    validated, reason = platform_onboarding._collector_aware_candidates(
        platform=Platform.YOUTUBE,
        candidates=[candidate],
        keywords=["уроки английского", "английский для начинающих", "преподаватель английского"],
        max_candidates=20,
        context=None,
    )

    assert [item.external_id for item in validated] == ["yt-english-club"]
    assert reason == ""


def test_candidate_with_strong_profile_but_offtopic_recent_shorts_is_still_dropped(monkeypatch):
    candidate = platform_onboarding._DiscoveryCandidate(
        platform=Platform.YOUTUBE,
        external_id="yt-skyeng",
        handle="skyeng",
        url="https://www.youtube.com/@skyeng",
        display_name="Skyeng – онлайн-школа иностранных языков",
        description="Онлайн-школа английского языка",
        query_hits={"онлайн школа английского", "уроки английского"},
        metadata={"rank_hint": 10000},
    )

    def fake_recent_youtube_short_signals(*, candidate, n, context=None):
        return [
            "Кто лучше ухаживает? Русские или французы?",
            "Ред флаги на свиданиях в Италии и Франции",
            "Как ухаживают итальянцы",
        ], [80000, 50000, 30000], 3

    monkeypatch.setattr(platform_onboarding, "_fetch_recent_youtube_short_signals", fake_recent_youtube_short_signals)

    validated, reason = platform_onboarding._collector_aware_candidates(
        platform=Platform.YOUTUBE,
        candidates=[candidate],
        keywords=["уроки английского", "онлайн школа английского", "английский для начинающих"],
        max_candidates=20,
        context=None,
    )

    assert validated == []
    assert "recent Shorts по теме" in reason


def test_collector_validation_batch_spreads_across_query_buckets():
    candidates = [
        platform_onboarding._DiscoveryCandidate(
            platform=Platform.YOUTUBE,
            external_id="yt-1",
            handle="teacher-a",
            url="https://www.youtube.com/@teacher-a",
            display_name="Teacher A",
            description="english teachers",
            query_hits={"преподаватель английского"},
            metadata={"rank_hint": 9000},
        ),
        platform_onboarding._DiscoveryCandidate(
            platform=Platform.YOUTUBE,
            external_id="yt-2",
            handle="teacher-b",
            url="https://www.youtube.com/@teacher-b",
            display_name="Teacher B",
            description="english teachers",
            query_hits={"преподаватель английского"},
            metadata={"rank_hint": 8000},
        ),
        platform_onboarding._DiscoveryCandidate(
            platform=Platform.YOUTUBE,
            external_id="yt-3",
            handle="lesson-c",
            url="https://www.youtube.com/@lesson-c",
            display_name="Lesson C",
            description="уроки английского",
            query_hits={"уроки английского"},
            metadata={"rank_hint": 1000},
        ),
    ]

    batch = platform_onboarding._balanced_validation_candidates(candidates, budget=2)

    assert [candidate.external_id for candidate in batch] == ["yt-1", "yt-3"]


def test_youtube_discovery_checks_multiple_candidates_before_returning_empty(monkeypatch):
    monkeypatch.setattr(
        platform_onboarding,
        "_discover_youtube_search_candidates",
        lambda **kwargs: [
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.YOUTUBE,
                external_id="yt-bad",
                handle="badchannel",
                url="https://www.youtube.com/@badchannel",
                display_name="Bad Channel",
                description="english teachers and lesson plans",
                query_hits={"english teachers"},
                metadata={"rank_hint": 900000},
            ),
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.YOUTUBE,
                external_id="yt-good",
                handle="goodchannel",
                url="https://www.youtube.com/@goodchannel",
                display_name="Good Channel",
                description="english teachers and lesson plans",
                query_hits={"english teachers"},
                metadata={"rank_hint": 1000},
            ),
        ],
    )
    monkeypatch.setattr(platform_onboarding, "_search_instagram_candidates_raw", lambda **kwargs: [])
    monkeypatch.setattr(platform_onboarding, "_search_tiktok_candidates_raw", lambda **kwargs: [])

    def fake_recent_youtube_short_signals(*, candidate, n, context=None):
        if candidate.external_id == "yt-good":
            return ["english teacher lesson plans", "worksheet ideas for english tutors"], [10000, 8700], 3
        return ["history exam tips", "егэ по истории"], [20000, 16000], 3

    monkeypatch.setattr(platform_onboarding, "_fetch_recent_youtube_short_signals", fake_recent_youtube_short_signals)

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
        (Platform.YOUTUBE, "yt-good"),
    ]


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

    assert first == ["Teacher worksheet ideas", "English lesson plan"]
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


def test_cached_instagram_search_results_uses_full_overreturned_pool(monkeypatch):
    clear_retry_cache()
    calls = {"search": 0}

    class FakeClient:
        def search_profiles(self, *, query, limit=None):
            calls["search"] += 1
            assert limit == 3
            return [
                {"id": f"ig-{idx}", "username": f"teacher_{idx}", "full_name": f"Teacher {idx}"}
                for idx in range(5)
            ]

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_get_instagram_client", lambda: FakeClient())

    results = platform_onboarding._cached_instagram_search_results(query="english tutors", limit=3, context=None)

    assert [item["id"] for item in results] == ["ig-0", "ig-1", "ig-2", "ig-3", "ig-4"]
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


def test_instagram_search_refetches_when_cached_pool_is_too_small_for_higher_limit(monkeypatch):
    clear_retry_cache()
    calls: list[int] = []

    class FakeClient:
        def search_profiles(self, *, query, limit=None):
            calls.append(int(limit or 0))
            if len(calls) == 1:
                return [
                    {"id": "ig-1", "username": "teacher_hub", "full_name": "Teacher Hub"},
                    {"id": "ig-2", "username": "lessonlab", "full_name": "Lesson Lab"},
                ]
            return [
                {"id": "ig-1", "username": "teacher_hub", "full_name": "Teacher Hub"},
                {"id": "ig-2", "username": "lessonlab", "full_name": "Lesson Lab"},
                {"id": "ig-3", "username": "teachernotes", "full_name": "Teacher Notes"},
                {"id": "ig-4", "username": "classroomclub", "full_name": "Classroom Club"},
            ]

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_get_instagram_client", lambda: FakeClient())

    first = platform_onboarding._cached_instagram_search_results(query="english tutors", limit=2, context=None)
    second = platform_onboarding._cached_instagram_search_results(query="english tutors", limit=4, context=None)

    assert [item["id"] for item in first] == ["ig-1", "ig-2"]
    assert [item["id"] for item in second] == ["ig-1", "ig-2", "ig-3", "ig-4"]
    assert calls == [2, 4]


def test_search_instagram_candidates_raw_uses_full_overreturned_pool_for_validation(monkeypatch):
    monkeypatch.setattr(platform_onboarding, "_discovery_queries", lambda **kwargs: ["english tutors"])
    monkeypatch.setattr(
        platform_onboarding,
        "_cached_instagram_search_results",
        lambda **kwargs: [
            {"id": f"ig-{idx}", "username": f"teacher_{idx}", "full_name": f"Teacher {idx}"}
            for idx in range(25)
        ],
    )
    monkeypatch.setattr(platform_onboarding, "fetch_instagram_profiles_cached", lambda **kwargs: [])

    candidates = platform_onboarding._search_instagram_candidates_raw(
        keywords=["english tutors"],
        competitors=[],
        max_candidates=20,
    )

    assert len(candidates) == 25
    assert {candidate.external_id for candidate in candidates} == {f"ig-{idx}" for idx in range(25)}


def test_search_instagram_candidates_raw_expands_related_profiles(monkeypatch):
    monkeypatch.setattr(platform_onboarding, "_discovery_queries", lambda **kwargs: ["english tutors"])
    monkeypatch.setattr(
        platform_onboarding,
        "_cached_instagram_search_results",
        lambda **kwargs: [
            {"id": "ig-1", "username": "teacher_hub", "full_name": "Teacher Hub"},
        ],
    )

    def fake_fetch_instagram_profiles_cached(*, inputs, context=None, purpose=None, context_id=None):
        items = []
        for lookup in inputs:
            handle = lookup.rstrip("/").split("/")[-1]
            if handle == "teacher_hub":
                items.append(
                    {
                        "id": "ig-1",
                        "username": "teacher_hub",
                        "url": "https://www.instagram.com/teacher_hub/",
                        "biography": "English teachers and lesson ideas",
                        "relatedProfiles": [
                            {
                                "id": "ig-2",
                                "username": "lesson_lab",
                                "full_name": "Lesson Lab",
                                "is_verified": True,
                            }
                        ],
                    }
                )
            elif handle == "lesson_lab":
                items.append(
                    {
                        "id": "ig-2",
                        "username": "lesson_lab",
                        "url": "https://www.instagram.com/lesson_lab/",
                        "biography": "Lesson ideas",
                        "relatedProfiles": [],
                    }
                )
        return items

    monkeypatch.setattr(platform_onboarding, "fetch_instagram_profiles_cached", fake_fetch_instagram_profiles_cached)

    candidates = platform_onboarding._search_instagram_candidates_raw(
        keywords=["english tutors"],
        competitors=[],
        max_candidates=20,
    )

    assert [candidate.external_id for candidate in candidates[:2]] == ["ig-2", "ig-1"]
    related = next(candidate for candidate in candidates if candidate.external_id == "ig-2")
    assert related.metadata["source"] == "related_profile"


def test_search_instagram_candidates_raw_uses_seed_related_profiles(monkeypatch):
    monkeypatch.setattr(platform_onboarding, "_discovery_queries", lambda **kwargs: ["productivity creator"])
    monkeypatch.setattr(platform_onboarding, "_cached_instagram_search_results", lambda **kwargs: [])

    def fake_fetch_instagram_profiles_cached(*, inputs, context=None, purpose=None, context_id=None):
        if purpose == "candidate_validation":
            return []
        assert purpose == "keyword_recent_content"
        return [
            {
                "id": "seed-ig",
                "username": "creatorlab",
                "url": "https://www.instagram.com/creatorlab/",
                "relatedProfiles": [
                    {
                        "id": "ig-2",
                        "username": "creatorsystems",
                        "full_name": "Creator Systems",
                        "is_verified": True,
                    }
                ],
            }
        ]

    monkeypatch.setattr(platform_onboarding, "fetch_instagram_profiles_cached", fake_fetch_instagram_profiles_cached)

    candidates = platform_onboarding._search_instagram_candidates_raw(
        keywords=["productivity creator"],
        competitors=[],
        seed_accounts=[
            _seed(
                platform=Platform.INSTAGRAM,
                external_id="seed-ig",
                handle="creatorlab",
                title="Creator Lab",
                description="productivity creator systems",
                url="https://www.instagram.com/creatorlab/",
            )
        ],
        max_candidates=20,
    )

    assert [candidate.external_id for candidate in candidates] == ["ig-2"]
    assert candidates[0].query_hits == {"seed_related"}


def test_search_instagram_candidates_raw_uses_full_search_budget(monkeypatch):
    calls: list[int] = []

    monkeypatch.setattr(
        platform_onboarding,
        "_discovery_queries",
        lambda **kwargs: ["tech review"],
    )
    monkeypatch.setattr(
        platform_onboarding,
        "_seed_instagram_related_candidates",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        platform_onboarding,
        "_expand_instagram_related_candidates",
        lambda **kwargs: kwargs["candidates"],
    )

    def fake_cached_instagram_search_results(*, query, limit, context=None, purpose=None, context_id=None):
        calls.append(limit)
        return [
            {"id": "ig-1", "username": "creator_one", "full_name": "Creator One"},
            {"id": "ig-2", "username": "creator_two", "full_name": "Creator Two"},
        ]

    monkeypatch.setattr(platform_onboarding, "_cached_instagram_search_results", fake_cached_instagram_search_results)

    candidates = platform_onboarding._search_instagram_candidates_raw(
        keywords=["tech review"],
        competitors=[],
        seed_accounts=[],
        max_candidates=20,
    )

    assert calls == [platform_onboarding._DISCOVERY_RESULT_BUDGET[Platform.INSTAGRAM]]
    assert [candidate.external_id for candidate in candidates] == ["ig-1", "ig-2"]


def test_expand_instagram_related_candidates_walks_second_graph_hop(monkeypatch):
    candidates = [
        platform_onboarding._DiscoveryCandidate(
            platform=Platform.INSTAGRAM,
            external_id="ig-seed-related",
            handle="seed_related_one",
            url="https://www.instagram.com/seed_related_one/",
            display_name="Seed Related",
            description="tech creator",
            query_hits={"seed_related"},
            metadata={"source": "related_profile", "graph_depth": 1, "graph_hits": 1},
        )
    ]

    def fake_fetch_instagram_profiles_cached(*, inputs, context=None, purpose=None, context_id=None):
        items = []
        for lookup in inputs:
            handle = lookup.rstrip("/").split("/")[-1]
            if handle == "seed_related_one":
                items.append(
                    {
                        "id": "ig-seed-related",
                        "username": "seed_related_one",
                        "url": "https://www.instagram.com/seed_related_one/",
                        "biography": "tech creator",
                        "relatedProfiles": [
                            {"id": "ig-hop-1", "username": "creator_hop_1", "full_name": "Creator Hop 1"},
                        ],
                    }
                )
            elif handle == "creator_hop_1":
                items.append(
                    {
                        "id": "ig-hop-1",
                        "username": "creator_hop_1",
                        "url": "https://www.instagram.com/creator_hop_1/",
                        "biography": "more tech",
                        "relatedProfiles": [
                            {"id": "ig-hop-2", "username": "creator_hop_2", "full_name": "Creator Hop 2"},
                        ],
                    }
                )
        return items

    monkeypatch.setattr(platform_onboarding, "fetch_instagram_profiles_cached", fake_fetch_instagram_profiles_cached)

    expanded = platform_onboarding._expand_instagram_related_candidates(candidates=candidates)

    by_id = {candidate.external_id: candidate for candidate in expanded}
    assert "ig-hop-1" in by_id
    assert "ig-hop-2" in by_id
    assert by_id["ig-hop-2"].metadata["graph_depth"] == 2


def test_expand_instagram_related_candidates_falls_back_on_profile_quota_error(monkeypatch):
    candidates = [
        platform_onboarding._DiscoveryCandidate(
            platform=Platform.INSTAGRAM,
            external_id="ig-1",
            handle="teacher_hub",
            url="https://www.instagram.com/teacher_hub/",
            display_name="Teacher Hub",
            description="English teacher",
            query_hits={"english tutors"},
        )
    ]

    def fake_fetch_instagram_profiles_cached(**kwargs):
        raise platform_onboarding.InstagramApiError("Monthly usage hard limit exceeded")

    monkeypatch.setattr(platform_onboarding, "fetch_instagram_profiles_cached", fake_fetch_instagram_profiles_cached)

    expanded = platform_onboarding._expand_instagram_related_candidates(candidates=candidates)

    assert expanded == candidates


def test_discover_competitors_for_onboarding_validates_multiple_ig_tt_candidates(monkeypatch):
    recent_instagram_ts = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    recent_tiktok_ts = (datetime.now(UTC) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    monkeypatch.setattr(platform_onboarding, "_discover_youtube_search_candidates", lambda **kwargs: [])
    monkeypatch.setattr(
        platform_onboarding,
        "_search_instagram_candidates_raw",
        lambda **kwargs: [
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.INSTAGRAM,
                external_id="ig-1",
                handle="teacherone",
                url="https://www.instagram.com/teacherone/",
                display_name="Teacher One",
                description="english teacher reels",
                query_hits={"english teachers"},
            ),
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.INSTAGRAM,
                external_id="ig-2",
                handle="teachertwo",
                url="https://www.instagram.com/teachertwo/",
                display_name="Teacher Two",
                description="english teacher reels",
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
                external_id="tt-1",
                handle="teachertok1",
                url="https://www.tiktok.com/@teachertok1",
                display_name="TeacherTok 1",
                description="english short lessons",
                query_hits={"english teachers"},
            ),
            platform_onboarding._DiscoveryCandidate(
                platform=Platform.TIKTOK,
                external_id="tt-2",
                handle="teachertok2",
                url="https://www.tiktok.com/@teachertok2",
                display_name="TeacherTok 2",
                description="english short lessons",
                query_hits={"english teachers"},
            ),
        ],
    )

    def fake_fetch_instagram_profiles_cached(*, inputs, context=None, purpose=None, context_id=None):
        out = []
        for lookup in inputs:
            handle = lookup.rstrip("/").split("/")[-1]
            out.append(
                {
                    "id": f"{handle}-id",
                    "username": handle,
                    "url": f"https://www.instagram.com/{handle}/",
                    "latestPosts": [
                        {
                            "id": f"{handle}-reel-1",
                            "productType": "clips",
                            "url": f"https://www.instagram.com/reel/{handle}-reel-1/",
                            "caption": "English teacher reel ideas",
                            "timestamp": recent_instagram_ts,
                            "videoViewCount": 2400,
                        },
                        {
                            "id": f"{handle}-reel-2",
                            "productType": "clips",
                            "url": f"https://www.instagram.com/reel/{handle}-reel-2/",
                            "caption": "English teacher classroom tips",
                            "timestamp": recent_instagram_ts,
                            "videoViewCount": 1800,
                        }
                    ],
                }
            )
        return out

    def fake_fetch_tiktok_profile_feeds_cached(*, handles, results_per_page, context=None, purpose=None, context_id=None):
        return {
            handle: [
                {
                    "id": f"{handle}-vid-1",
                    "text": "english teacher short lesson",
                    "createTimeISO": recent_tiktok_ts,
                    "authorMeta": {"id": f"{handle}-author", "name": handle, "nickName": handle},
                    "webVideoUrl": f"https://www.tiktok.com/@{handle}/video/{handle}-vid-1",
                    "videoMeta": {"duration": 19},
                    "playCount": 8800,
                    "diggCount": 200,
                    "commentCount": 11,
                    "shareCount": 4,
                },
                {
                    "id": f"{handle}-vid-2",
                    "text": "english teacher lesson ideas",
                    "createTimeISO": recent_tiktok_ts,
                    "authorMeta": {"id": f"{handle}-author", "name": handle, "nickName": handle},
                    "webVideoUrl": f"https://www.tiktok.com/@{handle}/video/{handle}-vid-2",
                    "videoMeta": {"duration": 24},
                    "playCount": 7600,
                    "diggCount": 160,
                    "commentCount": 9,
                    "shareCount": 3,
                }
            ]
            for handle in handles
        }

    monkeypatch.setattr(platform_onboarding, "fetch_instagram_profiles_cached", fake_fetch_instagram_profiles_cached)
    monkeypatch.setattr(platform_onboarding, "fetch_tiktok_profile_feeds_cached", fake_fetch_tiktok_profile_feeds_cached)

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
        max_youtube_search_calls=3,
        max_candidates_per_platform=20,
    )

    assert [(candidate.platform, candidate.handle) for candidate in outcome.candidates] == [
        (Platform.INSTAGRAM, "teacherone"),
        (Platform.INSTAGRAM, "teachertwo"),
        (Platform.TIKTOK, "teachertok1"),
        (Platform.TIKTOK, "teachertok2"),
    ]


def test_instagram_candidate_with_single_recent_reel_can_pass_validation(monkeypatch):
    candidate = platform_onboarding._DiscoveryCandidate(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="marketlab",
        url="https://www.instagram.com/marketlab/",
        display_name="Market Lab",
        description="stock market analysis and macro investing",
        query_hits={"stock market analysis", "macro investing"},
        metadata={"verified": True},
    )

    monkeypatch.setattr(
        platform_onboarding,
        "_batch_fetch_recent_instagram_reel_texts",
        lambda **kwargs: {
            "ig-1": (
                ["stock market analysis and macro investing"],
                [12000],
                1,
            )
        },
    )

    validated, reason = platform_onboarding._collector_aware_candidates(
        platform=Platform.INSTAGRAM,
        candidates=[candidate],
        keywords=["stock market analysis", "macro investing", "portfolio strategy"],
        max_candidates=20,
        context=None,
    )

    assert reason == ""
    assert [item.external_id for item in validated] == ["ig-1"]


def test_instagram_graph_sourced_creator_candidates_can_pass_with_broad_theme(monkeypatch):
    candidates = [
        platform_onboarding._DiscoveryCandidate(
            platform=Platform.INSTAGRAM,
            external_id="ig-1",
            handle="unboxtherapy",
            url="https://www.instagram.com/unboxtherapy/",
            display_name="Unbox Therapy",
            description="Where products get naked.",
            query_hits={"seed_related", "tech review"},
            metadata={"source": "related_profile", "graph_depth": 1, "graph_hits": 2},
        ),
        platform_onboarding._DiscoveryCandidate(
            platform=Platform.INSTAGRAM,
            external_id="ig-spam",
            handle="_samsung__galaxy_ultra_s26",
            url="https://www.instagram.com/_samsung__galaxy_ultra_s26/",
            display_name="Samsung Galaxy Ultra S26",
            description="",
            query_hits={"samsung galaxy s26 ultra", "tech review"},
            metadata={},
        ),
    ]

    monkeypatch.setattr(
        platform_onboarding,
        "_batch_fetch_recent_instagram_reel_texts",
        lambda **kwargs: {
            "ig-1": (
                [
                    "Early look at the new DJI 360 drone",
                    "This laptop does it all",
                    "Understand almost any language with these smart glasses",
                ],
                [200000, 150000, 125000],
                12,
            ),
            "ig-spam": (
                [
                    "Samsung Galaxy S26 Ultra",
                    "Samsung Galaxy S26 Ultra camera",
                ],
                [1200, 900],
                2,
            ),
        },
    )

    validated, reason = platform_onboarding._collector_aware_candidates(
        platform=Platform.INSTAGRAM,
        candidates=candidates,
        keywords=["samsung galaxy s26 ultra", "tech review", "macbook neo", "pro review"],
        max_candidates=20,
        context=None,
    )

    assert reason == ""
    assert [candidate.external_id for candidate in validated] == ["ig-1"]


def test_tiktok_search_refetches_when_cached_pool_is_too_small_for_higher_limit(monkeypatch):
    clear_retry_cache()
    calls: list[int] = []

    class FakeClient:
        def search_profiles(self, *, query, limit=None):
            calls.append(int(limit or 0))
            if len(calls) == 1:
                return [
                    {"id": "tt-1", "name": "teacherhub", "nickName": "Teacher Hub"},
                    {"id": "tt-2", "name": "lessonlab", "nickName": "Lesson Lab"},
                ]
            return [
                {"id": "tt-1", "name": "teacherhub", "nickName": "Teacher Hub"},
                {"id": "tt-2", "name": "lessonlab", "nickName": "Lesson Lab"},
                {"id": "tt-3", "name": "teachernotes", "nickName": "Teacher Notes"},
                {"id": "tt-4", "name": "classroomclub", "nickName": "Classroom Club"},
            ]

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_get_tiktok_client", lambda: FakeClient())

    first = platform_onboarding._cached_tiktok_search_results(query="english tutors", limit=2, context=None)
    second = platform_onboarding._cached_tiktok_search_results(query="english tutors", limit=4, context=None)

    assert [item["id"] for item in first] == ["tt-1", "tt-2"]
    assert [item["id"] for item in second] == ["tt-1", "tt-2", "tt-3", "tt-4"]
    assert calls == [2, 4]


def test_youtube_low_recall_rescue_requeries_and_merges_candidates(monkeypatch):
    seed = _seed(
        platform=Platform.YOUTUBE,
        external_id="yt-seed",
        handle="creator",
        title="Creator",
        description="English teacher",
        url="https://www.youtube.com/@creator",
    )

    initial_candidate = platform_onboarding._DiscoveryCandidate(
        platform=Platform.YOUTUBE,
        external_id="yt-1",
        handle="teacher-a",
        url="https://www.youtube.com/@teacher-a",
        display_name="Teacher A",
        description="english lessons",
        query_hits={"уроки английского"},
        metadata={"rank_hint": 1000},
    )
    rescue_candidate = platform_onboarding._DiscoveryCandidate(
        platform=Platform.YOUTUBE,
        external_id="yt-2",
        handle="teacher-b",
        url="https://www.youtube.com/@teacher-b",
        display_name="Teacher B",
        description="english teacher",
        query_hits={"english teacher"},
        metadata={"rank_hint": 900},
    )

    calls: list[list[str]] = []

    def fake_discover_youtube_search_candidates(*, keywords, competitors, max_search_calls, context=None):
        calls.append(list(keywords))
        if "english teacher" in keywords:
            return [initial_candidate, rescue_candidate]
        return [initial_candidate]

    def fake_collector_aware_candidates(*, platform, candidates, keywords, max_candidates, context=None):
        ids = sorted(candidate.external_id for candidate in candidates)
        if ids == ["yt-1"]:
            return [initial_candidate], ""
        return [initial_candidate, rescue_candidate], ""

    monkeypatch.setattr(
        platform_onboarding,
        "_discover_youtube_search_candidates",
        fake_discover_youtube_search_candidates,
    )
    monkeypatch.setattr(
        platform_onboarding,
        "_collector_aware_candidates",
        fake_collector_aware_candidates,
    )
    monkeypatch.setattr(
        platform_onboarding,
        "_youtube_fallback_queries",
        lambda keywords: ["english teacher"],
    )

    outcome = platform_onboarding.discover_competitors_for_onboarding(
        keywords=["уроки английского"],
        seed=seed,
        competitors=[],
        linked_accounts=[],
        max_youtube_search_calls=5,
        max_candidates_per_platform=20,
    )

    assert calls == [["уроки английского"], ["уроки английского", "english teacher"]]
    assert sorted((candidate.platform, candidate.handle) for candidate in outcome.candidates) == [
        (Platform.YOUTUBE, "teacher-a"),
        (Platform.YOUTUBE, "teacher-b"),
    ]


def test_progressive_validation_batches_scan_beyond_first_budget():
    candidates = [
        platform_onboarding._DiscoveryCandidate(
            platform=Platform.YOUTUBE,
            external_id=f"yt-{idx}",
            handle=f"teacher-{idx}",
            url=f"https://www.youtube.com/@teacher-{idx}",
            display_name=f"Teacher {idx}",
            description="english lessons",
            query_hits={f"query-{idx % 3}"},
            metadata={"rank_hint": 1000 - idx},
        )
        for idx in range(12)
    ]

    original_budget = platform_onboarding._DISCOVERY_VALIDATION_BUDGET[Platform.YOUTUBE]
    original_max_scan = platform_onboarding._DISCOVERY_VALIDATION_MAX_SCAN[Platform.YOUTUBE]
    platform_onboarding._DISCOVERY_VALIDATION_BUDGET[Platform.YOUTUBE] = 4
    platform_onboarding._DISCOVERY_VALIDATION_MAX_SCAN[Platform.YOUTUBE] = 10
    try:
        batches = platform_onboarding._progressive_validation_batches(
            platform=Platform.YOUTUBE,
            ranked_candidates=candidates,
        )
    finally:
        platform_onboarding._DISCOVERY_VALIDATION_BUDGET[Platform.YOUTUBE] = original_budget
        platform_onboarding._DISCOVERY_VALIDATION_MAX_SCAN[Platform.YOUTUBE] = original_max_scan

    assert [len(batch) for batch in batches] == [4, 4, 2]
    scanned_ids = [candidate.external_id for batch in batches for candidate in batch]
    assert len(scanned_ids) == len(set(scanned_ids)) == 10
