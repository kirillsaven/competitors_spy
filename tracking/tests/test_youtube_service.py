from __future__ import annotations

from django.test import override_settings

from tracking.adapters import youtube as youtube_adapter
from tracking.services import youtube_service
from tracking.services.setup_retry_cache import clear_retry_cache


def test_search_youtube_seed_candidates_reuses_retry_cache(monkeypatch):
    clear_retry_cache()
    calls = {"search": 0, "channels": 0}

    class FakeClient:
        def search_channels(self, *, q, max_results):
            calls["search"] += 1
            return ["UC123"]

        def channels_list(self, *, part, ids=None, for_handle=None):
            calls["channels"] += 1
            return [
                {
                    "id": "UC123",
                    "snippet": {
                        "title": "English Teacher Hub",
                        "description": "lesson plans",
                        "customUrl": "@englishteacherhub",
                    },
                    "contentDetails": {"relatedPlaylists": {"uploads": "UU123"}},
                }
            ]

        def close(self):
            return None

    monkeypatch.setattr(youtube_service, "get_youtube_client", lambda: FakeClient())

    first = youtube_service.search_youtube_seed_candidates(query="english teachers", max_results=5)
    second = youtube_service.search_youtube_seed_candidates(query="english teachers", max_results=5)

    assert [seed.external_id for seed in first] == ["UC123"]
    assert [seed.external_id for seed in second] == ["UC123"]
    assert calls == {"search": 1, "channels": 1}


@override_settings(YOUTUBE_API_KEY="", YOUTUBE_API_KEYS=["key-a", "key-b"])
def test_get_youtube_client_uses_key_pool():
    client = youtube_service.get_youtube_client()
    try:
        assert client.api_keys == ["key-a", "key-b"]
    finally:
        client.close()


def test_youtube_client_rotates_to_next_key_on_quota_exceeded(monkeypatch):
    requests: list[str] = []

    class FakeResponse:
        def __init__(self, status_code, data):
            self.status_code = status_code
            self._data = data

        def json(self):
            return self._data

    class FakeHttpClient:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, url, params):
            requests.append(params["key"])
            if params["key"] == "key-a":
                return FakeResponse(
                    403,
                    {
                        "error": {
                            "errors": [{"reason": "quotaExceeded"}],
                            "code": 403,
                            "message": "quota hit",
                        }
                    },
                )
            return FakeResponse(200, {"items": [{"id": "UC123"}]})

        def close(self):
            return None

    monkeypatch.setattr(youtube_adapter.httpx, "Client", FakeHttpClient)

    client = youtube_adapter.YouTubeClient(api_keys=["key-a", "key-b"])
    try:
        items = client.channels_list(part="snippet", ids=["UC123"])
    finally:
        client.close()

    assert requests == ["key-a", "key-b"]
    assert items == [{"id": "UC123"}]
    assert client.api_key == "key-b"
