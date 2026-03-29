from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tracking.adapters import registry
from tracking.models import Competitor, ContentItem, MetricSnapshot, Platform
from tracking.services import collector
from tracking.services.provider_runtime import ProviderFetchCache


def test_registry_returns_registered_handler(monkeypatch):
    handler = lambda **_: ["sentinel"]
    monkeypatch.setitem(registry._REFRESH_COMPETITOR_REGISTRY, Platform.YOUTUBE, handler)

    assert registry.get_refresh_competitor_handler(Platform.YOUTUBE) is handler


def test_registry_returns_registered_tiktok_handler():
    handler = registry.get_refresh_competitor_handler(Platform.TIKTOK)

    assert handler is collector.refresh_tiktok_competitor


def test_registry_returns_registered_instagram_handler():
    handler = registry.get_refresh_competitor_handler(Platform.INSTAGRAM)

    assert handler is collector.refresh_instagram_competitor


def test_registry_raises_for_unregistered_platform():
    with pytest.raises(registry.AdapterRegistryError, match="No refresh handler registered for platform: unknown"):
        registry.get_refresh_competitor_handler("unknown")


def test_refresh_competitor_dispatches_by_platform(monkeypatch):
    competitor = Competitor(platform=Platform.YOUTUBE, external_id="cid")
    captured_at = datetime.now(tz=UTC)
    seen: dict[str, object] = {}
    provider_fetch_cache = ProviderFetchCache()

    def handler(*, competitor, mode, captured_at, provider_fetch_cache):
        seen["competitor"] = competitor
        seen["mode"] = mode
        seen["captured_at"] = captured_at
        seen["provider_fetch_cache"] = provider_fetch_cache
        return ["ok"]

    monkeypatch.setattr(collector, "get_refresh_competitor_handler", lambda platform: handler)

    result = collector.refresh_competitor(
        competitor=competitor,
        mode="full",
        captured_at=captured_at,
        provider_fetch_cache=provider_fetch_cache,
    )

    assert result == ["ok"]
    assert seen == {
        "competitor": competitor,
        "mode": "full",
        "captured_at": captured_at,
        "provider_fetch_cache": provider_fetch_cache,
    }


def test_refresh_youtube_competitor_keeps_shorts_and_long_videos(db, monkeypatch):
    competitor = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="UCshorts123",
        handle="shorts-channel",
        meta={"uploads_playlist_id": "UUshorts123"},
    )

    class FakeClient:
        def playlist_items(self, *, playlist_id, max_results):
            assert playlist_id == "UUshorts123"
            return [
                {"contentDetails": {"videoId": "short-1"}},
                {"contentDetails": {"videoId": "long-1"}},
            ]

        def videos_list(self, *, ids, part):
            assert ids == ["short-1", "long-1"]
            return [
                {
                    "id": "short-1",
                    "snippet": {
                        "title": "Short lesson",
                        "description": "Short description",
                        "publishedAt": "2026-03-20T12:00:00Z",
                    },
                    "statistics": {"viewCount": "1200", "likeCount": "44", "commentCount": "5"},
                    "contentDetails": {"duration": "PT45S"},
                },
                {
                    "id": "long-1",
                    "snippet": {
                        "title": "Long lesson",
                        "description": "Long description",
                        "publishedAt": "2026-03-20T13:00:00Z",
                    },
                    "statistics": {"viewCount": "8200", "likeCount": "144", "commentCount": "15"},
                    "contentDetails": {"duration": "PT8M"},
                },
            ]

        def close(self):
            return None

    monkeypatch.setattr(collector, "_get_youtube_client", lambda: FakeClient())

    items = collector.refresh_youtube_competitor(
        competitor=competitor,
        mode="incremental",
        captured_at=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    assert [item.external_id for item in items] == ["short-1", "long-1"]
    short_item = ContentItem.objects.get(platform=Platform.YOUTUBE, external_id="short-1")
    long_item = ContentItem.objects.get(platform=Platform.YOUTUBE, external_id="long-1")
    short_snapshot = MetricSnapshot.objects.get(content_item=short_item)
    long_snapshot = MetricSnapshot.objects.get(content_item=long_item)
    assert short_item.meta["content_type"] == "short"
    assert long_item.meta["content_type"] == "video"
    assert short_snapshot.views == 1200
    assert long_snapshot.views == 8200


def test_refresh_youtube_competitor_keeps_long_videos_when_no_recent_shorts_exist(db, monkeypatch):
    competitor = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="UClong123",
        handle="long-channel",
        meta={"uploads_playlist_id": "UUlong123"},
    )

    class FakeClient:
        def playlist_items(self, *, playlist_id, max_results):
            return [{"contentDetails": {"videoId": "long-1"}}]

        def videos_list(self, *, ids, part):
            return [
                {
                    "id": "long-1",
                    "snippet": {
                        "title": "Long lesson",
                        "description": "Long description",
                        "publishedAt": "2026-03-20T13:00:00Z",
                    },
                    "statistics": {"viewCount": "8200", "likeCount": "144", "commentCount": "15"},
                    "contentDetails": {"duration": "PT8M"},
                }
            ]

        def close(self):
            return None

    monkeypatch.setattr(collector, "_get_youtube_client", lambda: FakeClient())

    items = collector.refresh_youtube_competitor(
        competitor=competitor,
        mode="incremental",
        captured_at=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    assert [item.external_id for item in items] == ["long-1"]
    content_item = ContentItem.objects.get(platform=Platform.YOUTUBE, external_id="long-1")
    snapshot = MetricSnapshot.objects.get(content_item=content_item)
    assert content_item.meta["content_type"] == "video"
    assert snapshot.views == 8200


def test_refresh_youtube_competitor_raises_when_no_recent_uploads_have_usable_metrics(db, monkeypatch):
    competitor = Competitor.objects.create(
        platform=Platform.YOUTUBE,
        external_id="UCempty123",
        handle="empty-channel",
        meta={"uploads_playlist_id": "UUempty123"},
    )

    class FakeClient:
        def playlist_items(self, *, playlist_id, max_results):
            return []

        def close(self):
            return None

    monkeypatch.setattr(collector, "_get_youtube_client", lambda: FakeClient())

    with pytest.raises(
        collector.CollectorError,
        match="YouTube channel returned no recent uploads with usable metrics: channel_id=UCempty123",
    ):
        collector.refresh_youtube_competitor(
            competitor=competitor,
            mode="incremental",
            captured_at=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
        )


def test_refresh_tiktok_competitor_raises_for_unsupported_provider(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.TIKTOK, external_id="tt-user", handle="tt-user")
    monkeypatch.setattr(
        collector,
        "fetch_tiktok_profile_feed_cached",
        lambda **kwargs: (_ for _ in ()).throw(collector.CollectorError("Unsupported TikTok provider: stub")),
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
        "fetch_tiktok_profile_feed_cached",
        lambda **kwargs: (_ for _ in ()).throw(collector.CollectorError("TIKTOK_PROVIDER_ACCESS_TOKEN is not set")),
    )

    with pytest.raises(collector.CollectorError, match="TIKTOK_PROVIDER_ACCESS_TOKEN is not set"):
        collector.refresh_tiktok_competitor(
            competitor=competitor,
            mode="incremental",
            captured_at=datetime.now(tz=UTC),
        )


def test_refresh_tiktok_competitor_raises_when_provider_returns_no_items(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.TIKTOK, external_id="tt-user", handle="apifytech")

    monkeypatch.setattr(collector, "fetch_tiktok_profile_feed_cached", lambda **kwargs: [])

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

    def fake_fetch_tiktok_profile_feed_cached(**kwargs):
        assert kwargs["handle"] == "apifytech"
        assert kwargs["results_per_page"] == 10
        return [sample_item]

    monkeypatch.setattr(collector, "fetch_tiktok_profile_feed_cached", fake_fetch_tiktok_profile_feed_cached)

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


def test_refresh_tiktok_competitor_uses_cached_feed_without_provider_call(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.TIKTOK, external_id="tt-user", handle="apifytech")
    sample_item = {
        "id": "7353646097262202145",
        "text": "Cached TikTok caption",
        "createTimeISO": "2024-04-03T14:22:40.000Z",
        "authorMeta": {
            "id": "7353570794285417504",
            "name": "apifytech",
            "nickName": "Apify Tech",
        },
        "webVideoUrl": "https://www.tiktok.com/@apifytech/video/7353646097262202145",
        "videoMeta": {"duration": 59},
        "diggCount": 725,
        "shareCount": 30,
        "playCount": 83900,
        "commentCount": 10,
    }
    cache = ProviderFetchCache()
    cache.store_tiktok_feed(handle="apifytech", items=[sample_item])

    def fail_tiktok_client():
        raise AssertionError("provider should not be called")

    monkeypatch.setattr(collector, "fetch_tiktok_profile_feed_cached", lambda **kwargs: fail_tiktok_client())

    items = collector.refresh_tiktok_competitor(
        competitor=competitor,
        mode="incremental",
        captured_at=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
        provider_fetch_cache=cache,
    )

    assert len(items) == 1
    assert ContentItem.objects.filter(platform=Platform.TIKTOK, external_id="7353646097262202145").exists()


def test_refresh_tiktok_competitor_truncates_overlong_title_but_keeps_full_description(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.TIKTOK, external_id="tt-user", handle="apifytech")
    long_caption = "T" * 700

    sample_item = {
        "id": "7353646097262202145",
        "text": long_caption,
        "createTimeISO": "2024-04-03T14:22:40.000Z",
        "authorMeta": {
            "id": "7353570794285417504",
            "name": "apifytech",
            "nickName": "Apify Tech",
        },
        "webVideoUrl": "https://www.tiktok.com/@apifytech/video/7353646097262202145",
        "videoMeta": {"duration": 59},
        "diggCount": 725,
        "shareCount": 30,
        "playCount": 83900,
        "commentCount": 10,
    }

    monkeypatch.setattr(collector, "fetch_tiktok_profile_feed_cached", lambda **kwargs: [sample_item])

    collector.refresh_tiktok_competitor(
        competitor=competitor,
        mode="incremental",
        captured_at=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    content_item = ContentItem.objects.get(platform=Platform.TIKTOK, external_id="7353646097262202145")
    assert len(content_item.title) == 500
    assert content_item.title == long_caption[:500]
    assert content_item.description == long_caption


def test_refresh_instagram_competitor_raises_for_unsupported_provider(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.INSTAGRAM, external_id="ig-user", handle="ig-user")
    monkeypatch.setattr(
        collector,
        "fetch_instagram_profiles_cached",
        lambda **kwargs: (_ for _ in ()).throw(collector.CollectorError("Unsupported Instagram provider: stub")),
    )

    with pytest.raises(collector.CollectorError, match="Unsupported Instagram provider: stub"):
        collector.refresh_instagram_competitor(
            competitor=competitor,
            mode="incremental",
            captured_at=datetime.now(tz=UTC),
        )


def test_refresh_instagram_competitor_raises_for_missing_credentials(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.INSTAGRAM, external_id="ig-user", handle="ig-user")
    monkeypatch.setattr(
        collector,
        "fetch_instagram_profiles_cached",
        lambda **kwargs: (_ for _ in ()).throw(collector.CollectorError("INSTAGRAM_PROVIDER_ACCESS_TOKEN is not set")),
    )

    with pytest.raises(collector.CollectorError, match="INSTAGRAM_PROVIDER_ACCESS_TOKEN is not set"):
        collector.refresh_instagram_competitor(
            competitor=competitor,
            mode="incremental",
            captured_at=datetime.now(tz=UTC),
        )


def test_refresh_instagram_competitor_raises_when_provider_returns_no_items(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.INSTAGRAM, external_id="ig-user", handle="apifytech")

    monkeypatch.setattr(collector, "fetch_instagram_profiles_cached", lambda **kwargs: [])

    with pytest.raises(collector.CollectorError, match="Instagram profile returned no items: lookup=apifytech"):
        collector.refresh_instagram_competitor(
            competitor=competitor,
            mode="incremental",
            captured_at=datetime.now(tz=UTC),
        )


def test_refresh_instagram_competitor_raises_when_profile_has_no_recent_reels(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.INSTAGRAM, external_id="ig-user", handle="apifytech")

    sample_profile = {
        "id": "7333333333333333333",
        "username": "apifytech",
        "url": "https://www.instagram.com/apifytech/",
        "latestPosts": [
            {
                "id": "3666666666666666666",
                "productType": "feed",
                "shortCode": "Dnolong123",
                "url": "https://www.instagram.com/p/Dnolong123/",
                "caption": "Feed video post",
                "timestamp": "2024-07-03T10:40:00.000Z",
                "videoDuration": 140,
                "videoViewCount": 44000,
                "likesCount": 1200,
                "commentsCount": 40,
            }
        ],
    }

    monkeypatch.setattr(collector, "fetch_instagram_profiles_cached", lambda **kwargs: [sample_profile])

    with pytest.raises(
        collector.CollectorError,
        match="Instagram profile returned no recent reels with views: username=apifytech",
    ):
        collector.refresh_instagram_competitor(
            competitor=competitor,
            mode="incremental",
            captured_at=datetime.now(tz=UTC),
        )


def test_refresh_instagram_competitor_persists_items_and_shares(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.INSTAGRAM, external_id="ig-user", handle="apifytech")

    sample_profile = {
        "id": "7333333333333333333",
        "username": "apifytech",
        "fullName": "Apify Tech",
        "biography": "web scraping, AI",
        "url": "https://www.instagram.com/apifytech/",
        "latestPosts": [
            {
                "id": "3555555555555555555",
                "type": "Video",
                "shortCode": "C9abc123xyz",
                "url": "https://www.instagram.com/reel/C9abc123xyz/",
                "caption": "Instagram reel caption",
                "timestamp": "2024-07-03T10:30:00.000Z",
                "videoDuration": 31,
                "videoViewCount": 124000,
                "likesCount": 930,
                "commentsCount": 18,
            }
        ],
    }

    def fake_fetch_instagram_profiles_cached(**kwargs):
        assert kwargs["inputs"] == ["apifytech"]
        return [sample_profile]

    monkeypatch.setattr(collector, "fetch_instagram_profiles_cached", fake_fetch_instagram_profiles_cached)

    items = collector.refresh_instagram_competitor(
        competitor=competitor,
        mode="incremental",
        captured_at=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    assert len(items) == 1
    content_item = ContentItem.objects.get(platform=Platform.INSTAGRAM, external_id="3555555555555555555")
    snapshot = MetricSnapshot.objects.get(content_item=content_item)
    competitor.refresh_from_db()
    assert competitor.display_name == "Apify Tech"
    assert competitor.url == "https://www.instagram.com/apifytech/"
    assert content_item.meta["content_type"] == "reel"
    assert snapshot.views == 124000
    assert snapshot.likes == 930
    assert snapshot.comments == 18
    assert snapshot.shares is None


def test_refresh_instagram_competitor_uses_cached_profile_without_provider_call(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.INSTAGRAM, external_id="ig-user", handle="apifytech")
    sample_profile = {
        "id": "7333333333333333333",
        "username": "apifytech",
        "fullName": "Apify Tech",
        "url": "https://www.instagram.com/apifytech/",
        "latestPosts": [
            {
                "id": "3555555555555555555",
                "type": "Video",
                "shortCode": "C9abc123xyz",
                "url": "https://www.instagram.com/reel/C9abc123xyz/",
                "caption": "Cached Instagram caption",
                "timestamp": "2024-07-03T10:30:00.000Z",
                "videoDuration": 31,
                "videoViewCount": 124000,
                "likesCount": 930,
                "commentsCount": 18,
            }
        ],
    }
    cache = ProviderFetchCache()
    cache.store_instagram_profile(profile=sample_profile, lookups=["apifytech"])

    def fail_instagram_client():
        raise AssertionError("provider should not be called")

    monkeypatch.setattr(collector, "fetch_instagram_profiles_cached", lambda **kwargs: fail_instagram_client())

    items = collector.refresh_instagram_competitor(
        competitor=competitor,
        mode="incremental",
        captured_at=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
        provider_fetch_cache=cache,
    )

    assert len(items) == 1
    assert ContentItem.objects.filter(platform=Platform.INSTAGRAM, external_id="3555555555555555555").exists()


def test_refresh_instagram_competitor_truncates_overlong_title_but_keeps_full_description(db, monkeypatch):
    competitor = Competitor.objects.create(platform=Platform.INSTAGRAM, external_id="ig-user", handle="apifytech")
    long_caption = "I" * 900

    sample_profile = {
        "id": "7333333333333333333",
        "username": "apifytech",
        "fullName": "Apify Tech",
        "url": "https://www.instagram.com/apifytech/",
        "latestPosts": [
            {
                "id": "3555555555555555555",
                "type": "Video",
                "shortCode": "C9abc123xyz",
                "url": "https://www.instagram.com/reel/C9abc123xyz/",
                "caption": long_caption,
                "timestamp": "2024-07-03T10:30:00.000Z",
                "videoDuration": 31,
                "videoViewCount": 124000,
                "likesCount": 930,
                "commentsCount": 18,
            }
        ],
    }

    monkeypatch.setattr(collector, "fetch_instagram_profiles_cached", lambda **kwargs: [sample_profile])

    collector.refresh_instagram_competitor(
        competitor=competitor,
        mode="incremental",
        captured_at=datetime(2026, 3, 24, 0, 0, tzinfo=UTC),
    )

    content_item = ContentItem.objects.get(platform=Platform.INSTAGRAM, external_id="3555555555555555555")
    assert len(content_item.title) == 500
    assert content_item.title == long_caption[:500]
    assert content_item.description == long_caption
