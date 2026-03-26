from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import monotonic

from django.conf import settings


@dataclass(frozen=True)
class _RetryCacheEntry:
    expires_at: float
    value: object


_CACHE: dict[str, _RetryCacheEntry] = {}
_LOCK = Lock()


def _ttl_seconds() -> int:
    return max(1, int(getattr(settings, "SETUP_RETRY_CACHE_TTL_SECONDS", 300) or 300))


def _prune_expired(*, now: float) -> None:
    expired = [key for key, entry in _CACHE.items() if entry.expires_at <= now]
    for key in expired:
        _CACHE.pop(key, None)


def get_cached_retry_value(key: str) -> tuple[object | None, bool]:
    cache_key = str(key or "").strip()
    if not cache_key:
        return None, False
    now = monotonic()
    with _LOCK:
        _prune_expired(now=now)
        entry = _CACHE.get(cache_key)
        if not entry:
            return None, False
        return entry.value, True


def store_retry_value(key: str, value: object, *, ttl_seconds: int | None = None) -> None:
    cache_key = str(key or "").strip()
    if not cache_key:
        return
    ttl = max(1, int(ttl_seconds if ttl_seconds is not None else _ttl_seconds()))
    now = monotonic()
    with _LOCK:
        _prune_expired(now=now)
        _CACHE[cache_key] = _RetryCacheEntry(expires_at=now + ttl, value=value)


def clear_retry_cache() -> None:
    with _LOCK:
        _CACHE.clear()
