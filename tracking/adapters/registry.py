from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from tracking.models import Platform

if TYPE_CHECKING:
    from tracking.models import Competitor, ContentItem


RefreshCompetitorHandler = Callable[..., list["ContentItem"]]


def _noop_refresh_competitor(*, competitor: "Competitor", mode: str, captured_at: datetime) -> list["ContentItem"]:
    return []


_REFRESH_COMPETITOR_REGISTRY: dict[str, RefreshCompetitorHandler] = {
    Platform.TIKTOK: _noop_refresh_competitor,
    Platform.INSTAGRAM: _noop_refresh_competitor,
}


def register_refresh_competitor_handler(platform: Platform, handler: RefreshCompetitorHandler) -> None:
    _REFRESH_COMPETITOR_REGISTRY[str(platform)] = handler


def get_refresh_competitor_handler(platform: str) -> RefreshCompetitorHandler:
    return _REFRESH_COMPETITOR_REGISTRY.get(platform, _noop_refresh_competitor)
