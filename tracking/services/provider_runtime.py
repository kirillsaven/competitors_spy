from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any
from uuid import uuid4


logger = logging.getLogger(__name__)


def _normalize_lookup(value: str | None) -> str:
    return str(value or "").strip().lower()


@dataclass
class ProviderFetchCache:
    context_id: str = field(default_factory=lambda: uuid4().hex)
    purpose: str = "report_collection"
    tiktok_feeds_by_handle: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    instagram_profiles_by_lookup: dict[str, dict[str, Any]] = field(default_factory=dict)

    def store_tiktok_feed(self, *, handle: str, items: list[dict[str, Any]]) -> None:
        key = _normalize_lookup(handle)
        if key:
            self.tiktok_feeds_by_handle[key] = [dict(item) for item in items]

    def get_tiktok_feed(self, *, handle: str) -> list[dict[str, Any]] | None:
        key = _normalize_lookup(handle)
        items = self.tiktok_feeds_by_handle.get(key)
        if items is None:
            return None
        return [dict(item) for item in items]

    def store_instagram_profile(self, *, profile: dict[str, Any], lookups: list[str]) -> None:
        profile_copy = dict(profile)
        keys = {
            _normalize_lookup(value)
            for value in [
                *lookups,
                profile.get("username"),
                profile.get("id"),
                profile.get("url"),
            ]
            if _normalize_lookup(value)
        }
        for key in keys:
            self.instagram_profiles_by_lookup[key] = profile_copy

    def get_instagram_profile(self, *, lookup: str) -> dict[str, Any] | None:
        key = _normalize_lookup(lookup)
        profile = self.instagram_profiles_by_lookup.get(key)
        if profile is None:
            return None
        return dict(profile)


def log_provider_call(
    *,
    actor: str,
    platform: str,
    purpose: str,
    cache: str,
    normalized_input: str,
    requested_limit: int | None,
    returned_count: int,
    context_id: str | None = None,
) -> None:
    logger.info(
        "provider_call actor=%s platform=%s purpose=%s cache=%s input=%s requested_limit=%s returned_count=%s context_id=%s",
        str(actor or ""),
        str(platform or ""),
        str(purpose or ""),
        str(cache or ""),
        str(normalized_input or ""),
        requested_limit,
        int(returned_count),
        str(context_id or ""),
    )
