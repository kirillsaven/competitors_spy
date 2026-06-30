from __future__ import annotations

import httpx
import pytest
from types import SimpleNamespace

from tracking.adapters.instagram import build_profile_url, extract_handle, profile_to_video_details, resolve_seed_input


def test_extract_handle_accepts_instagram_handles_and_urls():
    assert extract_handle("@apifytech") == "apifytech"
    assert extract_handle("apifytech") == "apifytech"
    assert extract_handle("https://www.instagram.com/apifytech/") == "apifytech"
    assert extract_handle("https://www.instagram.com/reel/C9abc123xyz/") is None


def test_resolve_seed_input_uses_profile_metadata():
    class FakeClient:
        def fetch_profiles(self, *, inputs):
            assert inputs == ["https://www.instagram.com/apifytech/"]
            return [
                {
                    "id": "7333333333333333333",
                    "username": "apifytech",
                    "fullName": "Apify Tech",
                    "biography": "web scraping, AI",
                    "url": "https://www.instagram.com/apifytech/",
                }
            ]

    seed = resolve_seed_input(FakeClient(), "https://www.instagram.com/apifytech/")

    assert seed is not None
    assert seed.external_id == "7333333333333333333"
    assert seed.handle == "apifytech"
    assert seed.url == "https://www.instagram.com/apifytech/"
    assert seed.title == "Apify Tech"
    assert seed.description == "web scraping, AI"


def test_resolve_seed_input_returns_none_when_provider_cannot_verify_profile():
    class FakeClient:
        def fetch_profiles(self, *, inputs):
            return [{"fullName": "Apify Tech"}]

    assert resolve_seed_input(FakeClient(), "https://www.instagram.com/apifytech/") is None


def test_profile_to_video_details_keeps_reels_without_views_for_interaction_ranking():
    details = profile_to_video_details(
        {
            "latestPosts": [
                {
                    "id": "3555555555555555555",
                    "productType": "clips",
                    "shortCode": "C9abc123xyz",
                    "url": "https://www.instagram.com/reel/C9abc123xyz/",
                    "caption": "Instagram reel caption",
                    "timestamp": "2024-07-03T10:30:00.000Z",
                    "videoDuration": 31,
                        "likesCount": 930,
                        "commentsCount": 18,
                },
                {
                    "id": "3666666666666666666",
                    "productType": "feed",
                    "shortCode": "Dnolong123",
                    "url": "https://www.instagram.com/p/Dnolong123/",
                    "caption": "Feed video post",
                    "timestamp": "2024-07-03T10:40:00.000Z",
                    "videoDuration": 140,
                    "videoViewCount": 44000,
                    "likesCount": 1200,
                    "commentsCount": 40,
                },
            ]
        }
    )

    assert len(details) == 1
    assert details[0].video_id == "3555555555555555555"
    assert details[0].url == "https://www.instagram.com/reel/C9abc123xyz/"
    assert details[0].title == "Instagram reel caption"
    assert details[0].views == 0
    assert details[0].views_available is False
    assert details[0].ranking_source == "interactions"
    assert details[0].likes == 930
    assert details[0].comments == 18
    assert details[0].shares is None


def test_profile_to_video_details_can_include_carousels_and_posts():
    details = profile_to_video_details(
        {
            "latestPosts": [
                {
                    "id": "carousel-1",
                    "productType": "carousel",
                    "url": "https://www.instagram.com/p/carousel-1/",
                    "caption": "Carousel caption",
                    "timestamp": "2024-07-03T10:30:00.000Z",
                    "likesCount": 900,
                    "commentsCount": 45,
                },
                {
                    "id": "post-1",
                    "productType": "feed",
                    "url": "https://www.instagram.com/p/post-1/",
                    "caption": "Feed caption",
                    "timestamp": "2024-07-03T10:40:00.000Z",
                    "likesCount": 120,
                    "commentsCount": 4,
                },
            ]
        },
        include_carousels=True,
        include_posts=True,
    )

    assert [item.content_type for item in details] == ["carousel", "post"]
    assert all(item.views == 0 for item in details)
    assert all(item.views_available is False for item in details)
    assert all(item.ranking_source == "interactions" for item in details)


def test_profile_to_video_details_supports_gwaa_public_profile_shape():
    details = profile_to_video_details(
        {
            "username": "anyagal",
            "pk": "8763809956",
            "posts": [
                {
                    "id": "3794699980469066210",
                    "code": "DSpe4btClni",
                    "taken_at": 1766583653,
                    "is_video": True,
                    "media_type": 2,
                    "product_type": "clips",
                    "caption": "9 видео, которые принесли +10 тыс подписчиков",
                    "play_count": 42000,
                    "like_count": 4216,
                    "comment_count": 138,
                },
                {
                    "id": "feed-1",
                    "code": "FeedOnly",
                    "taken_at": 1766583653,
                    "is_video": False,
                    "media_type": 1,
                    "product_type": "feed",
                    "caption": "Image post",
                    "like_count": 10,
                },
            ],
        }
    )

    assert len(details) == 1
    assert details[0].video_id == "3794699980469066210"
    assert details[0].url == "https://www.instagram.com/reel/DSpe4btClni/"
    assert details[0].views == 42000
    assert details[0].likes == 4216
    assert details[0].comments == 138


def test_gwaa_client_fetches_public_profiles():
    from tracking.adapters.instagram import GwaaInstagramClient

    seen: dict[str, object] = {}

    class FakeHttpClient:
        def get(self, url, params, headers):
            seen["url"] = url
            seen["params"] = params
            seen["headers"] = headers
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"username": "anyagal", "pk": "8763809956", "posts": []},
            )

        def close(self):
            return None

    client = GwaaInstagramClient(base_url="https://highlights.gwaa.net")
    client._client = FakeHttpClient()

    try:
        profiles = client.fetch_profiles(inputs=["https://www.instagram.com/anyagal/"])
    finally:
        client.close()

    assert seen["url"] == "https://highlights.gwaa.net/api/instagram/profile.php"
    assert seen["params"] == {"username": "anyagal"}
    assert profiles == [{"username": "anyagal", "pk": "8763809956", "posts": []}]


def test_gwaa_client_skips_missing_public_profiles():
    from tracking.adapters.instagram import GwaaInstagramClient

    class FakeHttpClient:
        def get(self, url, params, headers):
            return SimpleNamespace(
                status_code=404,
                json=lambda: {"success": False, "error": "Profile not found"},
            )

        def close(self):
            return None

    client = GwaaInstagramClient(base_url="https://highlights.gwaa.net")
    client._client = FakeHttpClient()

    try:
        profiles = client.fetch_profiles(inputs=["missingprofile"])
    finally:
        client.close()

    assert profiles == []


def test_resolve_seed_input_builds_profile_url_when_missing():
    class FakeClient:
        def fetch_profiles(self, *, inputs):
            return [
                {
                    "id": "7333333333333333333",
                    "username": "apifytech",
                    "fullName": "Apify Tech",
                }
            ]

    seed = resolve_seed_input(FakeClient(), "@apifytech")

    assert seed is not None
    assert seed.url == build_profile_url("apifytech")


def test_fetch_profiles_includes_usernames_for_profile_urls():
    from tracking.adapters.instagram import ApifyInstagramClient

    seen: dict[str, object] = {}

    class FakeHttpClient:
        def post(self, url, headers, json):
            seen["url"] = url
            seen["headers"] = headers
            seen["json"] = json
            return SimpleNamespace(status_code=200, json=lambda: [])

        def close(self):
            return None

    client = ApifyInstagramClient(access_token="token", actor_id="actor")
    client._client = FakeHttpClient()

    try:
        client.fetch_profiles(inputs=["https://www.instagram.com/apifytech/"])
    finally:
        client.close()

    assert seen["json"] == {
        "directUrls": ["https://www.instagram.com/apifytech/"],
        "usernames": ["apifytech"],
    }


def test_search_profiles_uses_configured_search_actor():
    from tracking.adapters.instagram import ApifyInstagramClient

    seen: dict[str, object] = {}

    class FakeHttpClient:
        def post(self, url, headers, json):
            seen["url"] = url
            seen["headers"] = headers
            seen["json"] = json
            return SimpleNamespace(status_code=200, json=lambda: [])

        def close(self):
            return None

    client = ApifyInstagramClient(access_token="token", actor_id="profile-actor", search_actor_id="search-actor")
    client._client = FakeHttpClient()

    try:
        client.search_profiles(query="english teachers")
    finally:
        client.close()

    assert seen["url"].endswith("/acts/search-actor/run-sync-get-dataset-items")
    assert seen["json"] == {"query": "english teachers"}


def test_fetch_profiles_wraps_transport_errors():
    from tracking.adapters.instagram import ApifyInstagramClient, InstagramApiError

    class FakeHttpClient:
        def post(self, url, headers, json):
            raise httpx.ReadTimeout("The read operation timed out")

        def close(self):
            return None

    client = ApifyInstagramClient(access_token="token", actor_id="profile-actor", search_actor_id="search-actor")
    client._client = FakeHttpClient()

    with pytest.raises(InstagramApiError, match="transport error"):
        client.fetch_profiles(inputs=["https://www.instagram.com/apifytech/"])


def test_search_profiles_wraps_transport_errors():
    from tracking.adapters.instagram import ApifyInstagramClient, InstagramApiError

    class FakeHttpClient:
        def post(self, url, headers, json):
            raise httpx.ReadTimeout("The read operation timed out")

        def close(self):
            return None

    client = ApifyInstagramClient(access_token="token", actor_id="profile-actor", search_actor_id="search-actor")
    client._client = FakeHttpClient()

    with pytest.raises(InstagramApiError, match="transport error"):
        client.search_profiles(query="english teachers")
