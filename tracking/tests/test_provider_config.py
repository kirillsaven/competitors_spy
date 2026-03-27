from __future__ import annotations

from django.test import override_settings

from tracking.models import Platform
from tracking.services.provider_config import get_instagram_apify_config, get_provider_config, get_tiktok_apify_config


@override_settings(
    TIKTOK_PROVIDER="stub",
    TIKTOK_PROVIDER_BASE_URL="",
    TIKTOK_PROVIDER_API_KEY="",
    TIKTOK_PROVIDER_API_SECRET="",
    TIKTOK_PROVIDER_ACCESS_TOKEN="",
    INSTAGRAM_PROVIDER="stub",
    INSTAGRAM_PROVIDER_BASE_URL="",
    INSTAGRAM_PROVIDER_API_KEY="",
    INSTAGRAM_PROVIDER_API_SECRET="",
    INSTAGRAM_PROVIDER_ACCESS_TOKEN="",
)
def test_provider_config_defaults_to_stub_for_future_platforms():
    tiktok = get_provider_config(Platform.TIKTOK)
    instagram = get_provider_config(Platform.INSTAGRAM)

    assert tiktok.provider == "stub"
    assert tiktok.base_url == ""
    assert instagram.provider == "stub"
    assert instagram.access_token == ""


@override_settings(
    TIKTOK_PROVIDER="rapidapi",
    TIKTOK_PROVIDER_BASE_URL="https://example.test/tiktok",
    TIKTOK_PROVIDER_API_KEY="tt-key",
    TIKTOK_PROVIDER_API_SECRET="tt-secret",
    TIKTOK_PROVIDER_ACCESS_TOKEN="tt-token",
)
def test_provider_config_reads_env_backed_settings():
    config = get_provider_config(Platform.TIKTOK)

    assert config.platform == Platform.TIKTOK
    assert config.provider == "rapidapi"
    assert config.base_url == "https://example.test/tiktok"
    assert config.api_key == "tt-key"
    assert config.api_secret == "tt-secret"
    assert config.access_token == "tt-token"


def test_provider_config_returns_builtin_for_non_stubbed_platform():
    config = get_provider_config(Platform.YOUTUBE)

    assert config.platform == Platform.YOUTUBE
    assert config.provider == "builtin"
    assert config.base_url == ""


@override_settings(
    TIKTOK_PROVIDER="apify",
    TIKTOK_PROVIDER_BASE_URL="",
    TIKTOK_PROVIDER_ACCESS_TOKEN="apify-token",
    TIKTOK_APIFY_PROFILE_ACTOR_ID="clockworks/custom-actor",
    TIKTOK_APIFY_SEARCH_ACTOR_ID="clockworks/custom-search",
)
def test_tiktok_apify_config_uses_default_fetch_limit():
    config = get_tiktok_apify_config()

    assert config.provider == "apify"
    assert config.base_url == "https://api.apify.com/v2"
    assert config.access_token == "apify-token"
    assert config.profile_actor_id == "clockworks/custom-actor"
    assert config.search_actor_id == "clockworks/custom-search"
    assert config.results_per_profile == 10


@override_settings(
    TIKTOK_PROVIDER="apify",
    TIKTOK_PROVIDER_BASE_URL="",
    TIKTOK_PROVIDER_ACCESS_TOKEN="apify-token",
    TIKTOK_APIFY_PROFILE_ACTOR_ID="clockworks/custom-actor",
    TIKTOK_APIFY_SEARCH_ACTOR_ID="clockworks/custom-search",
    TIKTOK_APIFY_RESULTS_PER_PROFILE=25,
)
def test_tiktok_apify_config_uses_custom_fetch_limit():
    config = get_tiktok_apify_config()

    assert config.provider == "apify"
    assert config.base_url == "https://api.apify.com/v2"
    assert config.access_token == "apify-token"
    assert config.profile_actor_id == "clockworks/custom-actor"
    assert config.search_actor_id == "clockworks/custom-search"
    assert config.results_per_profile == 25


@override_settings(
    INSTAGRAM_PROVIDER="apify",
    INSTAGRAM_PROVIDER_BASE_URL="",
    INSTAGRAM_PROVIDER_ACCESS_TOKEN="ig-token",
    INSTAGRAM_APIFY_PROFILE_ACTOR_ID="apify/custom-instagram-actor",
    INSTAGRAM_APIFY_SEARCH_ACTOR_ID="iron-crawler/custom-search",
)
def test_instagram_apify_config_uses_defaults_and_actor_settings():
    config = get_instagram_apify_config()

    assert config.provider == "apify"
    assert config.base_url == "https://api.apify.com/v2"
    assert config.access_token == "ig-token"
    assert config.profile_actor_id == "apify/custom-instagram-actor"
    assert config.search_actor_id == "iron-crawler/custom-search"
