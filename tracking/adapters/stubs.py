from __future__ import annotations

from .base import CompetitorCandidate, SeedResolution, VideoDetails


def resolve_seed_input(_: str) -> SeedResolution | None:
    return None


def discover_competitors(_: list[str], __: SeedResolution | None) -> list[CompetitorCandidate]:
    return []


def refresh_competitor(_: str, __: str) -> list[VideoDetails]:
    return []

