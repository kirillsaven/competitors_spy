from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from tracking.models import Platform


@dataclass(frozen=True)
class ProviderConfig:
    platform: str
    provider: str
    base_url: str
    api_key: str
    api_secret: str
    access_token: str


@dataclass(frozen=True)
class TikTokApifyConfig:
    provider: str
    base_url: str
    access_token: str
    actor_id: str
    results_per_profile: int


@dataclass(frozen=True)
class InstagramApifyConfig:
    provider: str
    base_url: str
    access_token: str
    actor_id: str


def get_provider_config(platform: str) -> ProviderConfig:
    platform_key = str(platform).strip().lower()
    if platform_key == Platform.TIKTOK:
        prefix = "TIKTOK_PROVIDER"
    elif platform_key == Platform.INSTAGRAM:
        prefix = "INSTAGRAM_PROVIDER"
    else:
        return ProviderConfig(
            platform=platform_key,
            provider="builtin",
            base_url="",
            api_key="",
            api_secret="",
            access_token="",
        )

    return ProviderConfig(
        platform=platform_key,
        provider=str(getattr(settings, prefix, "stub") or "stub"),
        base_url=str(getattr(settings, f"{prefix}_BASE_URL", "") or ""),
        api_key=str(getattr(settings, f"{prefix}_API_KEY", "") or ""),
        api_secret=str(getattr(settings, f"{prefix}_API_SECRET", "") or ""),
        access_token=str(getattr(settings, f"{prefix}_ACCESS_TOKEN", "") or ""),
    )


def get_tiktok_apify_config() -> TikTokApifyConfig:
    provider = get_provider_config(Platform.TIKTOK)
    return TikTokApifyConfig(
        provider=provider.provider,
        base_url=provider.base_url or "https://api.apify.com/v2",
        access_token=provider.access_token,
        actor_id=str(
            getattr(settings, "TIKTOK_APIFY_PROFILE_ACTOR_ID", "clockworks/tiktok-profile-scraper")
            or "clockworks/tiktok-profile-scraper"
        ),
        results_per_profile=max(1, int(getattr(settings, "TIKTOK_APIFY_RESULTS_PER_PROFILE", 15) or 15)),
    )


def get_instagram_apify_config() -> InstagramApifyConfig:
    provider = get_provider_config(Platform.INSTAGRAM)
    return InstagramApifyConfig(
        provider=provider.provider,
        base_url=provider.base_url or "https://api.apify.com/v2",
        access_token=provider.access_token,
        actor_id=str(
            getattr(settings, "INSTAGRAM_APIFY_PROFILE_ACTOR_ID", "apify/instagram-profile-scraper")
            or "apify/instagram-profile-scraper"
        ),
    )
