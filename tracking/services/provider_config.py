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
