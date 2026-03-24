from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tracking.adapters import registry
from tracking.models import Competitor, ContentItem, MetricSnapshot, Platform
from tracking.services import collector


def test_registry_returns_registered_handler(monkeypatch):
    handler = lambda **_: ["sentinel"]
    monkeypatch.setitem(registry._REFRESH_COMPETITOR_REGISTRY, Platform.YOUTUBE, handler)

    assert registry.get_refresh_competitor_handler(Platform.YOUTUBE) is handler


def test_registry_returns_registered_tiktok_handler():
    handler = registry.get_refresh_competitor_handler(Platform.TIKTOK)

    assert handler is collector.refresh_tiktok_competitor


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


def test_refresh_tiktok_competitor_raises_for_unsupported_provider(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.TIKTOK, external_id="tt-user", handle="tt-user")
    monkeypatch.setattr(
        collector,
        "get_tiktok_apify_config",
        lambda: SimpleNamespace(provider="stub", access_token="", actor_id="", base_url="", results_per_profile=10),
    )

    with pytest.raises(collector.CollectorError, match="Unsupported TikTok provider: stub"):
        collector.refresh_tiktok_competitor(
            competitor=competitor,
            mode="incremental",
            captured_at=datetime.now(tz=UTC),
        )


def test_refresh_tiktok_competitor_raises_for_missing_credentials(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.TIKTOK, external_id="tt-user", handle="tt-user")
    monkeypatch.setattr(
        collector,
        "get_tiktok_apify_config",
        lambda: SimpleNamespace(
            provider="apify",
            access_token="",
            actor_id="clockworks/tiktok-profile-scraper",
            base_url="https://api.apify.com/v2",
            results_per_profile=10,
        ),
    )

    with pytest.raises(collector.CollectorError, match="TIKTOK_PROVIDER_ACCESS_TOKEN is not set"):
        collector.refresh_tiktok_competitor(
            competitor=competitor,
            mode="incremental",
            captured_at=datetime.now(tz=UTC),
        )


def test_refresh_tiktok_competitor_raises_when_provider_returns_no_items(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.TIKTOK, external_id="tt-user", handle="apifytech")

    class FakeClient:
        def fetch_profile_feed(self, *, handle, results_per_page):
            return []

        def close(self):
            return None

    monkeypatch.setattr(
        collector,
        "get_tiktok_apify_config",
        lambda: SimpleNamespace(
            provider="apify",
            access_token="token",
            actor_id="actor",
            base_url="https://api.apify.com/v2",
            results_per_profile=10,
        ),
    )
    monkeypatch.setattr(collector, "_get_tiktok_client", lambda: FakeClient())

    with pytest.raises(collector.CollectorError, match="TikTok profile returned no items: handle=apifytech"):
        collector.refresh_tiktok_competitor(
            competitor=competitor,
            mode="incremental",
            captured_at=datetime.now(tz=UTC),
        )


def test_refresh_tiktok_competitor_persists_items_and_shares(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.TIKTOK, external_id="tt-user", handle="apifytech")

    sample_item = {
        "id": "7353646097262202145",
        "text": "TikTok caption",
        "createTimeISO": "2024-04-03T14:22:40.000Z",
        "authorMeta": {
            "id": "7353570794285417504",
            "name": "apifytech",
            "nickName": "Apify Tech",
            "signature": "web scraping, AI",
        },
        "webVideoUrl": "https://www.tiktok.com/@apifytech/video/7353646097262202145",
        "videoMeta": {"duration": 59},
        "diggCount": 725,
        "shareCount": 30,
        "playCount": 83900,
        "commentCount": 10,
        "isSlideshow": False,
    }

    class FakeClient:
        def fetch_profile_feed(self, *, handle, results_per_page):
            assert handle == "apifytech"
            assert results_per_page == 10
            return [sample_item]

        def close(self):
            return None

    monkeypatch.setattr(
        collector,
        "get_tiktok_apify_config",
        lambda: SimpleNamespace(provider="apify", access_token="token", actor_id="actor", base_url="url", results_per_profile=10),
    )
    monkeypatch.setattr(collector, "_get_tiktok_client", lambda: FakeClient())

    items = collector.refresh_tiktok_competitor(
        competitor=competitor,
        mode="incremental",
        captured_at=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    assert len(items) == 1
    content_item = ContentItem.objects.get(platform=Platform.TIKTOK, external_id="7353646097262202145")
    snapshot = MetricSnapshot.objects.get(content_item=content_item)
    competitor.refresh_from_db()
    assert competitor.display_name == "Apify Tech"
    assert competitor.url == "https://www.tiktok.com/@apifytech"
    assert snapshot.views == 83900
    assert snapshot.likes == 725
    assert snapshot.comments == 10
    assert snapshot.shares == 30
