from __future__ import annotations

from dataclasses import dataclass
import logging
import pickle
from threading import Lock
from time import monotonic

from django.conf import settings
from redis import Redis
from redis.exceptions import RedisError


@dataclass(frozen=True)
class _RetryCacheEntry:
    expires_at: float
    value: object


_CACHE: dict[str, _RetryCacheEntry] = {}
_LOCK = Lock()
_REDIS_CLIENT: Redis | None = None
_REDIS_FAILED = False
_logger = logging.getLogger(__name__)


def _ttl_seconds() -> int:
    return max(1, int(getattr(settings, "SETUP_RETRY_CACHE_TTL_SECONDS", 300) or 300))


def _redis_client() -> Redis | None:
    global _REDIS_CLIENT, _REDIS_FAILED
    if _REDIS_FAILED:
        return None
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    redis_url = str(getattr(settings, "REDIS_URL", "") or "").strip()
    if not redis_url:
        _REDIS_FAILED = True
        return None
    try:
        _REDIS_CLIENT = Redis.from_url(redis_url, socket_timeout=1.0, socket_connect_timeout=1.0)
        return _REDIS_CLIENT
    except Exception as exc:
        _REDIS_FAILED = True
        _logger.warning("Shared retry cache disabled: redis init failed: %s", exc)
        return None


def _redis_key(key: str) -> str:
    return f"retry-cache::{key}"


def _prune_expired(*, now: float) -> None:
    expired = [key for key, entry in _CACHE.items() if entry.expires_at <= now]
    for key in expired:
        _CACHE.pop(key, None)


def get_cached_retry_value(key: str) -> tuple[object | None, bool]:
    cache_key = str(key or "").strip()
    if not cache_key:
        return None, False
    client = _redis_client()
    if client is not None:
        try:
            payload = client.get(_redis_key(cache_key))
            if payload is not None:
                return pickle.loads(payload), True
        except (RedisError, pickle.PickleError, EOFError, AttributeError, ValueError, TypeError) as exc:
            _logger.warning("Shared retry cache read failed for %s: %s", cache_key, exc)
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
    client = _redis_client()
    if client is not None:
        try:
            client.setex(_redis_key(cache_key), ttl, pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
        except (RedisError, pickle.PickleError, TypeError, ValueError) as exc:
            _logger.warning("Shared retry cache write failed for %s: %s", cache_key, exc)
    now = monotonic()
    with _LOCK:
        _prune_expired(now=now)
        _CACHE[cache_key] = _RetryCacheEntry(expires_at=now + ttl, value=value)


def clear_retry_cache() -> None:
    client = _redis_client()
    if client is not None:
        try:
            for key in client.scan_iter(match=_redis_key("*")):
                client.delete(key)
        except RedisError as exc:
            _logger.warning("Shared retry cache clear failed: %s", exc)
    with _LOCK:
        _CACHE.clear()
