from __future__ import annotations

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


def test_profile_to_video_details_keeps_only_items_with_views():
    details = profile_to_video_details(
        {
            "latestPosts": [
                {
                    "id": "3555555555555555555",
                    "shortCode": "C9abc123xyz",
                    "url": "https://www.instagram.com/reel/C9abc123xyz/",
                    "caption": "Instagram reel caption",
                    "timestamp": "2024-07-03T10:30:00.000Z",
                    "videoDuration": 31,
                    "videoViewCount": 124000,
                    "likesCount": 930,
                    "commentsCount": 18,
                },
                {
                    "id": "3666666666666666666",
                    "shortCode": "Dnophoto123",
                    "url": "https://www.instagram.com/p/Dnophoto123/",
                    "caption": "Image post",
                    "timestamp": "2024-07-03T10:40:00.000Z",
                    "likesCount": 120,
                    "commentsCount": 4,
                },
            ]
        }
    )

    assert len(details) == 1
    assert details[0].video_id == "3555555555555555555"
    assert details[0].url == "https://www.instagram.com/reel/C9abc123xyz/"
    assert details[0].title == "Instagram reel caption"
    assert details[0].views == 124000
    assert details[0].likes == 930
    assert details[0].comments == 18
    assert details[0].shares is None


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
