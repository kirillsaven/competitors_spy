import pytest

from tracking.services import setup_retry_cache


@pytest.fixture(autouse=True)
def isolate_retry_cache(settings, monkeypatch):
    """Isolate bot and tracking unit tests from Docker DNS and shared cache state."""
    settings.REDIS_URL = ""
    monkeypatch.setattr(setup_retry_cache, "_REDIS_CLIENT", None)
    monkeypatch.setattr(setup_retry_cache, "_REDIS_FAILED", False)
    monkeypatch.setattr(setup_retry_cache, "_CACHE", {})
