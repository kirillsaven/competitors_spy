from __future__ import annotations

from tracking.adapters.base import SeedResolution
from tracking.models import Platform
from tracking.services import platform_onboarding


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
        seed=SeedResolution(
            platform=Platform.TIKTOK,
            external_id="tt-1",
            handle="apifytech",
            url="https://www.tiktok.com/@apifytech",
            title="Apify Tech",
            description="Automation",
            uploads_playlist_id=None,
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
        seed=SeedResolution(
            platform=Platform.INSTAGRAM,
            external_id="ig-1",
            handle="nasa",
            url="https://www.instagram.com/nasa/",
            title="NASA",
            description="Space agency",
            uploads_playlist_id=None,
        ),
        n=5,
    )

    assert texts == ["Mars update", "Moon update"]


def test_discover_instagram_competitors_uses_related_profiles(monkeypatch):
    responses = {
        "https://www.instagram.com/nasa/": [
            {
                "relatedProfiles": [
                    {"id": "ig-2", "username": "esa", "full_name": "ESA"},
                    {"id": "ig-3", "username": "jaxa_en", "full_name": "JAXA"},
                ]
            }
        ],
        "https://www.instagram.com/spacex/": [
            {
                "relatedProfiles": [
                    {"id": "ig-2", "username": "esa", "full_name": "ESA"},
                    {"id": "ig-4", "username": "blueorigin", "full_name": "Blue Origin"},
                ]
            }
        ],
    }

    class FakeClient:
        def fetch_profiles(self, *, inputs):
            return responses[inputs[0]]

        def close(self):
            return None

    monkeypatch.setattr(platform_onboarding, "_get_instagram_client", lambda: FakeClient())
    seed = SeedResolution(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="nasa",
        url="https://www.instagram.com/nasa/",
        title="NASA",
        description="Space",
        uploads_playlist_id=None,
    )
    competitor = SeedResolution(
        platform=Platform.INSTAGRAM,
        external_id="ig-manual",
        handle="spacex",
        url="https://www.instagram.com/spacex/",
        title="SpaceX",
        description="Rockets",
        uploads_playlist_id=None,
    )

    candidates = platform_onboarding.discover_instagram_competitors(seed=seed, competitors=[competitor], max_candidates=10)

    assert [(candidate.external_id, candidate.handle, candidate.reason) for candidate in candidates] == [
        ("ig-2", "esa", "related_comp,related_seed"),
        ("ig-3", "jaxa_en", "related_seed"),
        ("ig-4", "blueorigin", "related_comp"),
    ]


def test_discover_competitors_for_onboarding_reports_tiktok_limitations(monkeypatch):
    monkeypatch.setattr(
        platform_onboarding,
        "discover_youtube_competitors",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("YouTube discovery should not run for TikTok seeds")),
    )

    outcome = platform_onboarding.discover_competitors_for_onboarding(
        keywords=["basketball"],
        seed=SeedResolution(
            platform=Platform.TIKTOK,
            external_id="tt-1",
            handle="nba",
            url="https://www.tiktok.com/@nba",
            title="NBA",
            description="Basketball",
            uploads_playlist_id=None,
        ),
        competitors=[],
        max_youtube_search_calls=3,
        max_candidates_per_platform=20,
    )

    assert outcome.candidates == []
    assert outcome.notes == [
        "TikTok: текущий провайдер не отдает связанные профили, поэтому автоподбор пока недоступен."
    ]
