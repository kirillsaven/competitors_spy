from __future__ import annotations

from datetime import UTC, datetime

from tracking.adapters import registry
from tracking.models import Competitor, Platform
from tracking.services import collector


def test_registry_returns_registered_handler(monkeypatch):
    handler = lambda **_: ["sentinel"]
    monkeypatch.setitem(registry._REFRESH_COMPETITOR_REGISTRY, Platform.YOUTUBE, handler)

    assert registry.get_refresh_competitor_handler(Platform.YOUTUBE) is handler


def test_registry_returns_noop_handler_for_stub_platform():
    competitor = Competitor(platform=Platform.TIKTOK, external_id="stub")
    handler = registry.get_refresh_competitor_handler(Platform.TIKTOK)

    assert handler(
        competitor=competitor,
        mode="incremental",
        captured_at=datetime.now(tz=UTC),
    ) == []


def test_refresh_competitor_dispatches_by_platform(monkeypatch):
    competitor = Competitor(platform=Platform.YOUTUBE, external_id="cid")
    captured_at = datetime.now(tz=UTC)
    seen: dict[str, object] = {}

    def handler(*, competitor, mode, captured_at):
        seen["competitor"] = competitor
        seen["mode"] = mode
        seen["captured_at"] = captured_at
        return ["ok"]

    monkeypatch.setattr(collector, "get_refresh_competitor_handler", lambda platform: handler)

    result = collector.refresh_competitor(
        competitor=competitor,
        mode="full",
        captured_at=captured_at,
    )

    assert result == ["ok"]
    assert seen == {
        "competitor": competitor,
        "mode": "full",
        "captured_at": captured_at,
    }
