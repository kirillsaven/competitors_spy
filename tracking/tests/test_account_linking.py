from __future__ import annotations

from tracking.adapters.base import SeedResolution
from tracking.models import LinkedAccountSource, Platform, TgUser, UserLinkedAccount
from tracking.services import account_linking


def _seed(
    *,
    platform: str,
    external_id: str,
    handle: str | None,
    title: str | None,
    description: str | None = None,
    url: str | None = None,
) -> SeedResolution:
    return SeedResolution(
        platform=platform,
        external_id=external_id,
        handle=handle,
        url=url or f"https://example.com/{external_id}",
        title=title,
        description=description,
        uploads_playlist_id=None,
    )


def test_suggest_accounts_for_platform_uses_exact_and_normalized_handle_for_instagram(monkeypatch):
    seed = _seed(
        platform=Platform.TIKTOK,
        external_id="tt-1",
        handle="creator.official",
        title="Creator Official",
        description="Find us at https://creator.example",
    )

    monkeypatch.setattr(
        account_linking,
        "fetch_instagram_profiles_cached",
        lambda **kwargs: [
            {
                "id": "ig-1",
                "username": "creator_official",
                "fullName": "Creator Official",
                "biography": "Official account https://creator.example",
                "url": "https://www.instagram.com/creator_official/",
                "externalUrl": "https://creator.example",
            }
        ],
    )

    suggestion = account_linking.suggest_accounts_for_platform(seed=seed, target_platform=Platform.INSTAGRAM)

    assert suggestion.note is None
    assert [candidate.seed.external_id for candidate in suggestion.candidates] == ["ig-1"]
    assert "normalized_handle" in suggestion.candidates[0].signals
    assert any(signal.startswith("shared_domains:") for signal in suggestion.candidates[0].signals)


def test_suggest_accounts_for_platform_ranks_youtube_candidates_by_multiple_signals(monkeypatch):
    seed = _seed(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="creator",
        title="Creator Space Lab",
        description="creator.example and Mars science",
        url="https://www.instagram.com/creator/",
    )

    monkeypatch.setattr(account_linking, "resolve_seed_for_platform", lambda **kwargs: None)
    monkeypatch.setattr(
        account_linking,
        "search_youtube_seed_candidates",
        lambda **kwargs: [
            _seed(
                platform=Platform.YOUTUBE,
                external_id="yt-1",
                handle="creator-space",
                title="Creator Space Lab",
                description="Mars science and updates https://creator.example",
                url="https://www.youtube.com/@creator-space",
            ),
            _seed(
                platform=Platform.YOUTUBE,
                external_id="yt-2",
                handle="other",
                title="Other Channel",
                description="different topic",
                url="https://www.youtube.com/@other",
            ),
        ],
    )

    suggestion = account_linking.suggest_accounts_for_platform(seed=seed, target_platform=Platform.YOUTUBE)

    assert suggestion.note is None
    assert [candidate.seed.external_id for candidate in suggestion.candidates] == ["yt-1"]
    signals = suggestion.candidates[0].signals
    assert any(signal.startswith("display_similarity:") for signal in signals)
    assert any(signal.startswith("shared_tokens:") for signal in signals)
    assert any(signal.startswith("shared_domains:") for signal in signals)


def test_suggest_accounts_for_platform_uses_provider_hint_and_small_tiktok_input_set(monkeypatch):
    seed = _seed(
        platform=Platform.YOUTUBE,
        external_id="yt-1",
        handle="creator",
        title="Creator",
        description="TikTok: https://www.tiktok.com/@creator_live",
    )

    calls: list[tuple[tuple[str, ...], int]] = []
    monkeypatch.setattr(
        account_linking,
        "fetch_tiktok_profile_feeds_cached",
        lambda *, handles, results_per_page, context=None, purpose=None, context_id=None: (
            calls.append((tuple(handles), results_per_page)),
            {
                handle: [
                    {
                        "authorMeta": {
                            "id": "tt-1",
                            "name": "creator_live",
                            "nickName": "Creator",
                            "signature": "Official account. IG https://www.instagram.com/creator/",
                        }
                    }
                ]
                if handle == "creator_live"
                else []
                for handle in handles
            }
        )[1],
    )

    suggestion = account_linking.suggest_accounts_for_platform(seed=seed, target_platform=Platform.TIKTOK)

    assert suggestion.note is None
    assert [candidate.seed.external_id for candidate in suggestion.candidates] == ["tt-1"]
    assert "provider_hint" in suggestion.candidates[0].signals
    assert calls == [(("creator", "creator_live"), 1)]


def test_suggest_accounts_for_platforms_reuses_matcher_for_multiple_targets(monkeypatch):
    seed = _seed(platform=Platform.YOUTUBE, external_id="yt-1", handle="creator", title="Creator")
    calls = {"instagram": 0, "tiktok": 0}

    class FakeMatcher:
        def __init__(self, *, seed, max_candidates, context=None):
            self.seed = seed
            self.max_candidates = max_candidates

        def suggest_for_platforms(self, target_platforms):
            output = {}
            for platform in target_platforms:
                calls[platform] += 1
                output[platform] = account_linking.LinkedAccountSuggestion(platform=platform, candidates=[], note="none")
            return output

    monkeypatch.setattr(account_linking, "CheapAccountMatcher", FakeMatcher)

    suggestions = account_linking.suggest_accounts_for_platforms(
        seed=seed,
        target_platforms=[Platform.INSTAGRAM, Platform.TIKTOK],
        max_candidates=2,
    )

    assert set(suggestions) == {Platform.INSTAGRAM, Platform.TIKTOK}
    assert calls == {"instagram": 1, "tiktok": 1}


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
                "signals": ["normalized_handle", "provider_hint"],
                "is_seed": False,
            }
        },
    )

    rows = list(UserLinkedAccount.objects.filter(user=user).order_by("platform"))
    assert len(rows) == 1
    assert rows[0].platform == Platform.INSTAGRAM
    assert rows[0].external_id == "ig-1"
    assert rows[0].source == LinkedAccountSource.AUTO
    assert rows[0].match_signals == ["normalized_handle", "provider_hint"]
