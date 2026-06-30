from __future__ import annotations

import json

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


def test_export_instagram_competitor_spy_supports_gwaa_without_token(tmp_path, monkeypatch, settings):
    from tracking.management.commands import export_instagram_competitor_spy as command_module

    settings.INSTAGRAM_PROVIDER = "gwaa"
    settings.INSTAGRAM_PROVIDER_ACCESS_TOKEN = ""
    settings.INSTAGRAM_PROVIDER_BASE_URL = ""

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == "https://highlights.gwaa.net"

        def fetch_profiles(self, *, inputs):
            assert inputs == ["anyagal"]
            return [
                {
                    "username": "anyagal",
                    "pk": "8763809956",
                    "posts": [
                        {
                            "id": "reel-public",
                            "code": "PublicReel",
                            "taken_at": 1766583653,
                            "product_type": "clips",
                            "caption": "Как делать триггерные заголовки",
                            "play_count": 0,
                            "like_count": 4216,
                            "comment_count": 138,
                        }
                    ],
                }
            ]

        def close(self):
            return None

    monkeypatch.setattr(command_module, "GwaaInstagramClient", FakeClient)
    output_path = tmp_path / "instagram_spy_public.md"

    call_command(
        "export_instagram_competitor_spy",
        "--instagram",
        "anyagal",
        "--format",
        "markdown",
        "--output",
        str(output_path),
    )

    text = output_path.read_text(encoding="utf-8")

    assert "@anyagal" in text
    assert "PublicReel" in text
    assert "Adapt the mechanism, not the wording" in text


def test_export_instagram_competitor_spy_continue_on_error_and_diagnostics(tmp_path, monkeypatch, settings):
    from tracking.adapters.instagram import InstagramApiError
    from tracking.management.commands import export_instagram_competitor_spy as command_module

    settings.INSTAGRAM_PROVIDER = "apify"
    settings.INSTAGRAM_PROVIDER_ACCESS_TOKEN = "token"

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def fetch_profiles(self, *, inputs):
            if inputs == ["blocked"]:
                raise InstagramApiError("status=403")
            assert len(inputs) == 1
            return [
                {
                    "username": inputs[0],
                    "latestReels": [
                        {
                            "id": "reel-no-views",
                            "productType": "clips",
                            "url": "https://www.instagram.com/reel/reel-no-views/",
                            "caption": "No views but real interactions",
                            "timestamp": "2026-06-03T10:00:00.000Z",
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
    diagnostics_path = tmp_path / "diagnostics.json"

    call_command(
        "export_instagram_competitor_spy",
        "--instagram",
        "okprofile",
        "--instagram",
        "blocked",
        "--continue-on-error",
        "--min-successful-profiles",
        "1",
        "--min-output-items",
        "1",
        "--diagnostics-output",
        str(diagnostics_path),
        "--format",
        "json",
        "--output",
        str(output_path),
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))

    assert payload["items"][0]["views"] == 0
    assert payload["items"][0]["views_available"] is False
    assert payload["items"][0]["ranking_source"] == "interactions"
    assert diagnostics["provider_status"]["okprofile"]["status"] == "ok"
    assert diagnostics["provider_status"]["blocked"]["status"] == "failed"


def test_export_instagram_competitor_spy_includes_carousels_when_requested(tmp_path, monkeypatch, settings):
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
                    "latestPosts": [
                        {
                            "id": "carousel-1",
                            "productType": "carousel",
                            "url": "https://www.instagram.com/p/carousel-1/",
                            "caption": "Useful carousel",
                            "timestamp": "2026-06-03T10:00:00.000Z",
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
        "creator",
        "--include-carousels",
        "--format",
        "json",
        "--output",
        str(output_path),
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert payload["items"][0]["content_type"] == "carousel"
    assert payload["items"][0]["ranking_source"] == "interactions"
