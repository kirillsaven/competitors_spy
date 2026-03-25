from __future__ import annotations

from tracking.adapters.base import SeedResolution
from tracking.models import LinkedAccountSource, Platform, TgUser, UserLinkedAccount
from tracking.services import account_linking


def _seed(*, platform: str, external_id: str, handle: str | None, title: str | None) -> SeedResolution:
    return SeedResolution(
        platform=platform,
        external_id=external_id,
        handle=handle,
        url=f"https://example.com/{external_id}",
        title=title,
        description=title,
        uploads_playlist_id=None,
    )


def test_suggest_accounts_for_platform_uses_exact_handle_for_instagram(monkeypatch):
    seed = _seed(platform=Platform.TIKTOK, external_id="tt-1", handle="creator", title="Creator Name")
    instagram = _seed(platform=Platform.INSTAGRAM, external_id="ig-1", handle="creator", title="Creator Name")

    def fake_resolve_seed_for_platform(*, platform: str, raw_input: str):
        assert platform == Platform.INSTAGRAM
        assert raw_input == "creator"
        return instagram

    monkeypatch.setattr(account_linking, "resolve_seed_for_platform", fake_resolve_seed_for_platform)

    suggestion = account_linking.suggest_accounts_for_platform(seed=seed, target_platform=Platform.INSTAGRAM)

    assert suggestion.note is None
    assert len(suggestion.candidates) == 1
    assert suggestion.candidates[0].seed == instagram
    assert "exact_handle" in suggestion.candidates[0].signals


def test_suggest_accounts_for_platform_filters_youtube_search_candidates(monkeypatch):
    seed = _seed(platform=Platform.INSTAGRAM, external_id="ig-1", handle="creator", title="Creator Space Lab")

    def fake_resolve_seed_for_platform(*, platform: str, raw_input: str):
        assert platform == Platform.YOUTUBE
        assert raw_input == "creator"
        return None

    monkeypatch.setattr(account_linking, "resolve_seed_for_platform", fake_resolve_seed_for_platform)
    monkeypatch.setattr(
        account_linking,
        "search_youtube_seed_candidates",
        lambda **kwargs: [
            _seed(platform=Platform.YOUTUBE, external_id="yt-1", handle="other", title="Creator Space Lab"),
            _seed(platform=Platform.YOUTUBE, external_id="yt-2", handle="other", title="Completely Different"),
        ],
    )

    suggestion = account_linking.suggest_accounts_for_platform(seed=seed, target_platform=Platform.YOUTUBE)

    assert suggestion.note is None
    assert [candidate.seed.external_id for candidate in suggestion.candidates] == ["yt-1"]
    assert "exact_display_name" in suggestion.candidates[0].signals


def test_replace_user_linked_accounts_replaces_existing_rows(db):
    user = TgUser.objects.create(tg_user_id=111, tg_chat_id=111)
    UserLinkedAccount.objects.create(
        user=user,
        platform=Platform.YOUTUBE,
        external_id="old-yt",
        handle="old",
        url="https://youtube.example/old",
        display_name="Old",
        source=LinkedAccountSource.SEED,
        is_seed=True,
        match_signals=["seed_exact_resolve"],
    )

    account_linking.replace_user_linked_accounts(
        user=user,
        accounts_by_platform={
            Platform.INSTAGRAM: {
                "platform": Platform.INSTAGRAM,
                "external_id": "ig-1",
                "handle": "creator",
                "url": "https://instagram.com/creator/",
                "title": "Creator",
                "source": LinkedAccountSource.AUTO,
                "signals": ["exact_handle"],
                "is_seed": False,
            }
        },
    )

    rows = list(UserLinkedAccount.objects.filter(user=user).order_by("platform"))
    assert len(rows) == 1
    assert rows[0].platform == Platform.INSTAGRAM
    assert rows[0].external_id == "ig-1"
    assert rows[0].source == LinkedAccountSource.AUTO
    assert rows[0].match_signals == ["exact_handle"]
