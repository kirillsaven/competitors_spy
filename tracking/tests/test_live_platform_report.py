from __future__ import annotations

from types import SimpleNamespace

from tracking.models import Platform
from tracking.services import live_platform_report


def test_prepare_live_platform_user_prefetches_tiktok_and_instagram_payloads(db, monkeypatch):
    monkeypatch.setattr(
        live_platform_report,
        "resolve_seed_for_platform",
        lambda *, platform, raw_input: SimpleNamespace(
            platform=platform,
            external_id=f"{platform}-id",
            handle="nba" if platform == Platform.TIKTOK else "nasa",
            url=f"https://example.com/{platform}",
            title=platform.upper(),
            uploads_playlist_id=None,
        ),
    )
    monkeypatch.setattr(
        live_platform_report,
        "fetch_tiktok_profile_feed_cached",
        lambda **kwargs: [{"id": "tt-item"}],
    )
    monkeypatch.setattr(
        live_platform_report,
        "fetch_instagram_profiles_cached",
        lambda **kwargs: [{"id": "ig-1", "username": "nasa", "url": "https://www.instagram.com/nasa/"}],
    )

    prepared = live_platform_report.prepare_live_platform_user(
        tg_user_id=1,
        tg_chat_id=1,
        timezone_str="UTC",
        entries=[(Platform.TIKTOK, "nba"), (Platform.INSTAGRAM, "nasa")],
    )

    assert prepared.provider_fetch_cache.get_tiktok_feed(handle="nba") == [{"id": "tt-item"}]
    assert prepared.provider_fetch_cache.get_instagram_profile(lookup="nasa") == {
        "id": "ig-1",
        "username": "nasa",
        "url": "https://www.instagram.com/nasa/",
    }
