from __future__ import annotations

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
