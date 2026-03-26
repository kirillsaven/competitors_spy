from __future__ import annotations

import httpx
import pytest

from tracking.adapters.tiktok import build_profile_url, extract_handle, item_to_video_details, resolve_seed_input


def test_extract_handle_accepts_tiktok_handles_and_urls():
    assert extract_handle("@apifytech") == "apifytech"
    assert extract_handle("apifytech") == "apifytech"
    assert extract_handle("https://www.tiktok.com/@apifytech") == "apifytech"
    assert extract_handle("https://www.youtube.com/@apifytech") is None


def test_resolve_seed_input_uses_author_metadata():
    class FakeClient:
        def fetch_profile_feed(self, *, handle, results_per_page):
            assert handle == "apifytech"
            assert results_per_page == 1
            return [
                {
                    "authorMeta": {
                        "id": "7353570794285417504",
                        "name": "apifytech",
                        "nickName": "Apify Tech",
                        "signature": "web scraping, AI",
                    }
                }
            ]

    seed = resolve_seed_input(FakeClient(), "https://www.tiktok.com/@apifytech")

    assert seed is not None
    assert seed.external_id == "7353570794285417504"
    assert seed.handle == "apifytech"
    assert seed.url == build_profile_url("apifytech")
    assert seed.title == "Apify Tech"
    assert seed.description == "web scraping, AI"


def test_resolve_seed_input_returns_none_when_provider_returns_no_items():
    class FakeClient:
        def fetch_profile_feed(self, *, handle, results_per_page):
            assert handle == "apifytech"
            assert results_per_page == 1
            return []

    assert resolve_seed_input(FakeClient(), "https://www.tiktok.com/@apifytech") is None


def test_resolve_seed_input_returns_none_when_provider_cannot_verify_profile():
    class FakeClient:
        def fetch_profile_feed(self, *, handle, results_per_page):
            assert handle == "apifytech"
            assert results_per_page == 1
            return [{"authorMeta": {"nickName": "Apify Tech"}}]

    assert resolve_seed_input(FakeClient(), "https://www.tiktok.com/@apifytech") is None


def test_item_to_video_details_maps_apify_fields():
    details = item_to_video_details(
        {
            "id": "7353646097262202145",
            "text": "TikTok caption",
            "createTimeISO": "2024-04-03T14:22:40.000Z",
            "authorMeta": {"name": "apifytech"},
            "webVideoUrl": "https://www.tiktok.com/@apifytech/video/7353646097262202145",
            "videoMeta": {"duration": 59},
            "diggCount": 725,
            "shareCount": 30,
            "playCount": 83900,
            "commentCount": 10,
        }
    )

    assert details.video_id == "7353646097262202145"
    assert details.title == "TikTok caption"
    assert details.url == "https://www.tiktok.com/@apifytech/video/7353646097262202145"
    assert details.duration_seconds == 59
    assert details.views == 83900
    assert details.likes == 725
    assert details.comments == 10
    assert details.shares == 30


def test_search_profiles_uses_search_queries_payload():
    from tracking.adapters.tiktok import ApifyTikTokClient

    seen: dict[str, object] = {}

    class FakeHttpClient:
        def post(self, url, headers, json):
            seen["url"] = url
            seen["headers"] = headers
            seen["json"] = json
            return type("Response", (), {"status_code": 200, "json": staticmethod(lambda: [])})()

        def close(self):
            return None

    client = ApifyTikTokClient(access_token="token", actor_id="profile-actor", search_actor_id="search-actor")
    client._client = FakeHttpClient()

    try:
        client.search_profiles(query="english teacher")
    finally:
        client.close()

    assert seen["url"].endswith("/acts/search-actor/run-sync-get-dataset-items")
    assert seen["json"] == {"searchQueries": ["english teacher"]}


def test_fetch_profile_feeds_wraps_transport_errors():
    from tracking.adapters.tiktok import ApifyTikTokClient, TikTokApiError

    class FakeHttpClient:
        def post(self, url, headers, json):
            raise httpx.ReadTimeout("The read operation timed out")

        def close(self):
            return None

    client = ApifyTikTokClient(access_token="token", actor_id="profile-actor", search_actor_id="search-actor")
    client._client = FakeHttpClient()

    with pytest.raises(TikTokApiError, match="transport error"):
        client.fetch_profile_feeds(handles=["teacherhub"], results_per_profile=3)


def test_search_profiles_wraps_transport_errors():
    from tracking.adapters.tiktok import ApifyTikTokClient, TikTokApiError

    class FakeHttpClient:
        def post(self, url, headers, json):
            raise httpx.ReadTimeout("The read operation timed out")

        def close(self):
            return None

    client = ApifyTikTokClient(access_token="token", actor_id="profile-actor", search_actor_id="search-actor")
    client._client = FakeHttpClient()

    with pytest.raises(TikTokApiError, match="transport error"):
        client.search_profiles(query="english teacher")
