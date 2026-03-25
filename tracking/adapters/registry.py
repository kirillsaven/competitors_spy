from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tracking.models import Competitor, ContentItem


RefreshCompetitorHandler = Callable[..., list["ContentItem"]]


class AdapterRegistryError(RuntimeError):
    pass


_REFRESH_COMPETITOR_REGISTRY: dict[str, RefreshCompetitorHandler] = {}


def register_refresh_competitor_handler(platform: str, handler: RefreshCompetitorHandler) -> None:
    _REFRESH_COMPETITOR_REGISTRY[str(platform)] = handler


def get_refresh_competitor_handler(platform: str) -> RefreshCompetitorHandler:
    handler = _REFRESH_COMPETITOR_REGISTRY.get(str(platform))
    if handler is None:
        raise AdapterRegistryError(f"No refresh handler registered for platform: {platform}")
    return handler
