from __future__ import annotations

import pytest

from tracking.adapters.base import SeedResolution
from tracking.models import Platform
from tracking.services import seed_resolver
from tracking.services.setup_retry_cache import clear_retry_cache


def test_candidate_platforms_for_exact_seed_detects_platform_specific_inputs():
    assert seed_resolver.candidate_platforms_for_exact_seed("https://www.tiktok.com/@apifytech") == [Platform.TIKTOK]
    assert seed_resolver.candidate_platforms_for_exact_seed("https://www.instagram.com/apifytech/") == [Platform.INSTAGRAM]
    assert seed_resolver.candidate_platforms_for_exact_seed("https://www.youtube.com/@MrBeast") == [Platform.YOUTUBE]


def test_can_search_youtube_seed_candidates_only_for_non_exact_queries():
    assert seed_resolver.can_search_youtube_seed_candidates("Mr Beast")
    assert not seed_resolver.can_search_youtube_seed_candidates("@mrbeast")
    assert not seed_resolver.can_search_youtube_seed_candidates("https://www.instagram.com/mrbeast/")


def test_resolve_exact_seed_returns_selectable_multi_platform_candidates(monkeypatch):
    monkeypatch.setattr(
        seed_resolver,
        "attempt_exact_seed_resolution",
        lambda raw_input, context=None: [
            seed_resolver.SeedResolveAttempt(
                platform=Platform.TIKTOK,
                seed=SeedResolution(
                    platform=Platform.TIKTOK,
                    external_id="tt-1",
                    handle="shared",
                    url="https://www.tiktok.com/@shared",
                    title="Shared TikTok",
                    description=None,
                    uploads_playlist_id=None,
                ),
            ),
            seed_resolver.SeedResolveAttempt(
                platform=Platform.INSTAGRAM,
                seed=SeedResolution(
                    platform=Platform.INSTAGRAM,
                    external_id="ig-1",
                    handle="shared",
                    url="https://www.instagram.com/shared/",
                    title="Shared Instagram",
                    description=None,
                    uploads_playlist_id=None,
                ),
            ),
        ],
    )

    with pytest.raises(seed_resolver.SeedResolveAmbiguity, match="pick the intended profile") as exc:
        seed_resolver.resolve_exact_seed("@shared")

    assert [seed.platform for seed in exc.value.candidates] == [Platform.INSTAGRAM, Platform.TIKTOK]


def test_resolve_seed_for_platform_reuses_retry_cache(monkeypatch):
    clear_retry_cache()
    calls = {"instagram": 0}

    def fake_fetch_profiles_cached(*, inputs, context=None, purpose=None, context_id=None):
        calls["instagram"] += 1
        return [
            {
                "id": "ig-1",
                "username": "dariapancho",
                "url": "https://www.instagram.com/dariapancho/",
                "fullName": "Daria Pancho",
                "biography": "English teacher",
            }
        ]

    monkeypatch.setattr(seed_resolver, "fetch_instagram_profiles_cached", fake_fetch_profiles_cached)

    first = seed_resolver.resolve_seed_for_platform(platform=Platform.INSTAGRAM, raw_input="dariapancho")
    second = seed_resolver.resolve_seed_for_platform(platform=Platform.INSTAGRAM, raw_input="dariapancho")

    assert first is not None
    assert second is not None
    assert first.handle == "dariapancho"
    assert second.handle == "dariapancho"
    assert calls == {"instagram": 1}


def test_resolve_exact_seed_surfaces_provider_errors(monkeypatch):
    monkeypatch.setattr(
        seed_resolver,
        "attempt_exact_seed_resolution",
        lambda raw_input: [
            seed_resolver.SeedResolveAttempt(
                platform=Platform.TIKTOK,
                error="TIKTOK_PROVIDER_ACCESS_TOKEN is not set",
            )
        ],
    )

    with pytest.raises(seed_resolver.SeedResolveError, match="TIKTOK_PROVIDER_ACCESS_TOKEN is not set"):
        seed_resolver.resolve_exact_seed("https://www.tiktok.com/@apifytech")


def test_resolve_instagram_seeds_batch_keeps_input_order_and_missing_entries(monkeypatch):
    calls: list[list[str]] = []

    def fake_fetch_instagram_profiles_cached(*, inputs, context=None, purpose=None):
        calls.append(list(inputs))
        return [
            {
                "id": "ig-1",
                "username": "eng.lisaa",
                "url": "https://www.instagram.com/eng.lisaa/",
                "fullName": "Eng Lisaa",
                "biography": "Teacher",
            },
            {
                "id": "ig-3",
                "username": "physics.world",
                "url": "https://www.instagram.com/physics.world/",
                "fullName": "Physics World",
                "biography": "Physics",
            },
        ]

    monkeypatch.setattr(seed_resolver, "fetch_instagram_profiles_cached", fake_fetch_instagram_profiles_cached)

    resolved = seed_resolver.resolve_instagram_seeds_batch(
        [
            "https://www.instagram.com/eng.lisaa/?igsh=abc111",
            "https://www.instagram.com/missing.account/?igsh=abc222",
            "https://www.instagram.com/physics.world/",
        ]
    )

    assert calls == [
        [
            "https://www.instagram.com/eng.lisaa/?igsh=abc111",
            "https://www.instagram.com/missing.account/?igsh=abc222",
            "https://www.instagram.com/physics.world/",
        ]
    ]
    assert [item.handle if item else None for item in resolved] == ["eng.lisaa", None, "physics.world"]
