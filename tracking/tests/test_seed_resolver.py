from __future__ import annotations

import pytest

from tracking.adapters.base import SeedResolution
from tracking.models import Platform
from tracking.services import seed_resolver


def test_candidate_platforms_for_exact_seed_detects_platform_specific_inputs():
    assert seed_resolver.candidate_platforms_for_exact_seed("https://www.tiktok.com/@apifytech") == [Platform.TIKTOK]
    assert seed_resolver.candidate_platforms_for_exact_seed("https://www.instagram.com/apifytech/") == [Platform.INSTAGRAM]
    assert seed_resolver.candidate_platforms_for_exact_seed("https://www.youtube.com/@MrBeast") == [Platform.YOUTUBE]


def test_can_search_youtube_seed_candidates_only_for_non_exact_queries():
    assert seed_resolver.can_search_youtube_seed_candidates("Mr Beast")
    assert not seed_resolver.can_search_youtube_seed_candidates("@mrbeast")
    assert not seed_resolver.can_search_youtube_seed_candidates("https://www.instagram.com/mrbeast/")


def test_resolve_exact_seed_raises_for_multi_platform_ambiguity(monkeypatch):
    monkeypatch.setattr(
        seed_resolver,
        "attempt_exact_seed_resolution",
        lambda raw_input: [
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

    with pytest.raises(seed_resolver.SeedResolveError, match="multiple platforms"):
        seed_resolver.resolve_exact_seed("@shared")


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
