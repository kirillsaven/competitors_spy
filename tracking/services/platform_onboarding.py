from __future__ import annotations

from dataclasses import dataclass

from tracking.adapters.base import CompetitorCandidate, SeedResolution
from tracking.adapters.instagram import (
    ApifyInstagramClient,
    build_profile_url as build_instagram_profile_url,
    profile_to_video_details,
)
from tracking.adapters.tiktok import ApifyTikTokClient, item_to_video_details
from tracking.models import Platform
from tracking.services.provider_config import get_instagram_apify_config, get_tiktok_apify_config
from tracking.services.youtube_service import discover_youtube_competitors, get_recent_video_titles


class PlatformOnboardingError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscoveryOutcome:
    candidates: list[CompetitorCandidate]
    notes: list[str]


def _get_tiktok_client() -> ApifyTikTokClient:
    config = get_tiktok_apify_config()
    if config.provider != "apify":
        raise PlatformOnboardingError(f"Unsupported TikTok provider: {config.provider}")
    if not config.access_token:
        raise PlatformOnboardingError("TIKTOK_PROVIDER_ACCESS_TOKEN is not set")
    if not config.actor_id:
        raise PlatformOnboardingError("TIKTOK_APIFY_PROFILE_ACTOR_ID is not set")
    if not config.base_url:
        raise PlatformOnboardingError("TIKTOK_PROVIDER_BASE_URL is not set")
    return ApifyTikTokClient(
        access_token=config.access_token,
        actor_id=config.actor_id,
        base_url=config.base_url,
    )


def _get_instagram_client() -> ApifyInstagramClient:
    config = get_instagram_apify_config()
    if config.provider != "apify":
        raise PlatformOnboardingError(f"Unsupported Instagram provider: {config.provider}")
    if not config.access_token:
        raise PlatformOnboardingError("INSTAGRAM_PROVIDER_ACCESS_TOKEN is not set")
    if not config.actor_id:
        raise PlatformOnboardingError("INSTAGRAM_APIFY_PROFILE_ACTOR_ID is not set")
    if not config.base_url:
        raise PlatformOnboardingError("INSTAGRAM_PROVIDER_BASE_URL is not set")
    return ApifyInstagramClient(
        access_token=config.access_token,
        actor_id=config.actor_id,
        base_url=config.base_url,
    )


def _get_tiktok_handle(seed: SeedResolution) -> str:
    handle = str(seed.handle or "").strip()
    if handle:
        return handle
    if seed.url and "/@" in seed.url:
        return seed.url.rstrip("/").split("/@")[-1].split("/", 1)[0].strip()
    raise PlatformOnboardingError("TikTok seed has no resolvable handle for analysis")


def _get_instagram_lookup(seed: SeedResolution) -> str:
    if seed.url:
        return seed.url.strip()
    if seed.handle:
        return seed.handle.strip()
    if seed.external_id:
        return seed.external_id.strip()
    raise PlatformOnboardingError("Instagram seed has no resolvable lookup value for analysis")


def get_recent_seed_content_texts(*, seed: SeedResolution, n: int = 10) -> list[str]:
    if seed.platform == Platform.YOUTUBE:
        return get_recent_video_titles(seed, n=n)

    if seed.platform == Platform.TIKTOK:
        client = _get_tiktok_client()
        try:
            items = client.fetch_profile_feed(handle=_get_tiktok_handle(seed), results_per_page=max(1, n))
        finally:
            client.close()
        texts: list[str] = []
        for item in items[:n]:
            details = item_to_video_details(item)
            text = details.description or details.title
            if text:
                texts.append(text)
        return texts

    if seed.platform == Platform.INSTAGRAM:
        client = _get_instagram_client()
        try:
            profiles = client.fetch_profiles(inputs=[_get_instagram_lookup(seed)])
        finally:
            client.close()
        if not profiles:
            return []
        texts: list[str] = []
        for details in profile_to_video_details(profiles[0])[:n]:
            text = details.description or details.title
            if text:
                texts.append(text)
        return texts

    return []


def discover_instagram_competitors(
    *,
    seed: SeedResolution,
    competitors: list[SeedResolution],
    max_candidates: int = 20,
) -> list[CompetitorCandidate]:
    client = _get_instagram_client()
    try:
        sources: list[tuple[SeedResolution, str]] = [(seed, "related_seed")]
        for competitor in competitors[:3]:
            if competitor.platform == Platform.INSTAGRAM:
                sources.append((competitor, "related_comp"))

        reason_by_id: dict[str, set[str]] = {}
        candidate_by_id: dict[str, CompetitorCandidate] = {}
        for source, reason in sources:
            profiles = client.fetch_profiles(inputs=[_get_instagram_lookup(source)])
            if not profiles:
                continue
            related = profiles[0].get("relatedProfiles") or []
            for item in related:
                external_id = str(item.get("id") or "").strip()
                username = str(item.get("username") or "").strip()
                if not external_id or not username:
                    continue
                if external_id == seed.external_id:
                    continue
                reason_by_id.setdefault(external_id, set()).add(reason)
                if external_id not in candidate_by_id:
                    candidate_by_id[external_id] = CompetitorCandidate(
                        platform=Platform.INSTAGRAM,
                        external_id=external_id,
                        handle=username,
                        url=build_instagram_profile_url(username),
                        display_name=str(item.get("full_name") or username),
                        reason="",
                    )
        out: list[CompetitorCandidate] = []
        for external_id, candidate in candidate_by_id.items():
            out.append(
                CompetitorCandidate(
                    platform=candidate.platform,
                    external_id=candidate.external_id,
                    handle=candidate.handle,
                    url=candidate.url,
                    display_name=candidate.display_name,
                    reason=",".join(sorted(reason_by_id.get(external_id, set()))) or "auto",
                )
            )
        return out[:max_candidates]
    finally:
        client.close()


def discover_competitors_for_onboarding(
    *,
    keywords: list[str],
    seed: SeedResolution | None,
    competitors: list[SeedResolution],
    max_youtube_search_calls: int,
) -> DiscoveryOutcome:
    candidates: list[CompetitorCandidate] = []
    notes: list[str] = []

    youtube_seed = seed if seed and seed.platform == Platform.YOUTUBE and seed.external_id else None
    youtube_competitors = [item for item in competitors if item.platform == Platform.YOUTUBE and item.external_id]
    if youtube_seed or youtube_competitors:
        try:
            candidates.extend(
                discover_youtube_competitors(
                    keywords=keywords,
                    seed=youtube_seed,
                    max_search_calls=max_youtube_search_calls,
                    max_candidates=20,
                    extra_featured_channel_ids=[item.external_id for item in youtube_competitors],
                )
            )
        except Exception as exc:
            notes.append(f"YouTube: автоподбор не сработал ({exc}).")

    instagram_seed = seed if seed and seed.platform == Platform.INSTAGRAM and seed.external_id else None
    instagram_competitors = [item for item in competitors if item.platform == Platform.INSTAGRAM and item.external_id]
    if instagram_seed:
        try:
            instagram_candidates = discover_instagram_competitors(
                seed=instagram_seed,
                competitors=instagram_competitors,
                max_candidates=20,
            )
        except Exception as exc:
            notes.append(f"Instagram: автоподбор не сработал ({exc}).")
        else:
            if instagram_candidates:
                candidates.extend(instagram_candidates)
            else:
                notes.append("Instagram: провайдер не вернул связанные профили для автоподбора.")

    if seed and seed.platform == Platform.TIKTOK:
        notes.append("TikTok: текущий провайдер не отдает связанные профили, поэтому автоподбор пока недоступен.")

    return DiscoveryOutcome(candidates=candidates, notes=notes)
