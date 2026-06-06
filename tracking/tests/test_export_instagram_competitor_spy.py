from __future__ import annotations

from django.core.management import call_command


def test_export_instagram_competitor_spy_writes_markdown(tmp_path, monkeypatch, settings):
    from tracking.management.commands import export_instagram_competitor_spy as command_module

    settings.INSTAGRAM_PROVIDER = "apify"
    settings.INSTAGRAM_PROVIDER_ACCESS_TOKEN = "token"
    settings.INSTAGRAM_PROVIDER_BASE_URL = "https://api.apify.com/v2"
    settings.INSTAGRAM_APIFY_PROFILE_ACTOR_ID = "profile-actor"
    settings.INSTAGRAM_APIFY_SEARCH_ACTOR_ID = "search-actor"

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def fetch_profiles(self, *, inputs):
            assert inputs == ["anyagal"]
            return [
                {
                    "username": "anyagal",
                    "latestPosts": [
                        {
                            "id": "low",
                            "productType": "clips",
                            "url": "https://www.instagram.com/reel/low/",
                            "caption": "Как сделать хук для Reels",
                            "timestamp": "2026-06-01T10:00:00.000Z",
                            "videoDuration": 21,
                            "videoViewCount": 12000,
                            "likesCount": 600,
                            "commentsCount": 30,
                        },
                        {
                            "id": "top",
                            "productType": "clips",
                            "url": "https://www.instagram.com/reel/top/",
                            "caption": "Главная ошибка в первом кадре",
                            "timestamp": "2026-06-02T10:00:00.000Z",
                            "videoDuration": 18,
                            "videoViewCount": 98000,
                            "likesCount": 3000,
                            "commentsCount": 80,
                        },
                    ],
                }
            ]

        def close(self):
            return None

    monkeypatch.setattr(command_module, "ApifyInstagramClient", FakeClient)
    output_path = tmp_path / "instagram_spy.md"

    call_command(
        "export_instagram_competitor_spy",
        "--instagram",
        "anyagal",
        "--limit",
        "1",
        "--format",
        "markdown",
        "--output",
        str(output_path),
    )

    text = output_path.read_text(encoding="utf-8")

    assert "# Instagram Competitor Spy Export" in text
    assert "@anyagal" in text
    assert "98000" in text
    assert "mistake / prohibition" in text
    assert "Adapt the mechanism, not the wording, for Daria's English tutor audience" in text
    assert "https://www.instagram.com/reel/top/" in text
    assert "https://www.instagram.com/reel/low/" not in text


def test_export_instagram_competitor_spy_writes_json(tmp_path, monkeypatch, settings):
    from tracking.management.commands import export_instagram_competitor_spy as command_module

    settings.INSTAGRAM_PROVIDER = "apify"
    settings.INSTAGRAM_PROVIDER_ACCESS_TOKEN = "token"

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def fetch_profiles(self, *, inputs):
            return [
                {
                    "username": "creator",
                    "latestReels": [
                        {
                            "id": "reel-1",
                            "productType": "clips",
                            "url": "https://www.instagram.com/reel/reel-1/",
                            "caption": "Before vs after hook",
                            "timestamp": "2026-06-03T10:00:00.000Z",
                            "videoDuration": 20,
                            "videoViewCount": 50000,
                            "likesCount": 1000,
                            "commentsCount": 50,
                        }
                    ],
                }
            ]

        def close(self):
            return None

    monkeypatch.setattr(command_module, "ApifyInstagramClient", FakeClient)
    output_path = tmp_path / "instagram_spy.json"

    call_command(
        "export_instagram_competitor_spy",
        "--instagram",
        "@creator",
        "--format",
        "json",
        "--output",
        str(output_path),
    )

    text = output_path.read_text(encoding="utf-8")

    assert '"inputs": [' in text
    assert '"rank": 1' in text
    assert '"competitor": "@creator"' in text
    assert '"mechanism_guess": "contrast / before-after"' in text
