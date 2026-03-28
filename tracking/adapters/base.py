from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SeedResolution:
    platform: str
    external_id: str  # channelId
    handle: str | None
    url: str
    title: str | None
    description: str | None
    uploads_playlist_id: str | None
    subscriber_count: int | None = None
    image_url: str | None = None


@dataclass(frozen=True)
class CompetitorCandidate:
    platform: str
    external_id: str  # channelId
    handle: str | None
    url: str
    display_name: str | None
    reason: str


@dataclass(frozen=True)
class VideoDetails:
    video_id: str
    url: str
    title: str
    description: str
    published_at: datetime
    duration_seconds: int | None
    views: int
    likes: int | None
    comments: int | None
    shares: int | None = None

