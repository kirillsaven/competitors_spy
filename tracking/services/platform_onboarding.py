from __future__ import annotations

from dataclasses import dataclass

from tracking.adapters.base import CompetitorCandidate, SeedResolution
from tracking.adapters.instagram import (
    ApifyInstagramClient,
    build_profile_url as build_instagram_profile_url,
)
from tracking.adapters.tiktok import ApifyTikTokClient, item_to_video_details
from tracking.models import Platform
from tracking.services.provider_config import get_instagram_apify_config, get_tiktok_apify_config
from tracking.services.youtube_service import discover_youtube_competitors, get_recent_video_titles


class PlatformOnboardingError(RuntimeError):
    pass


DISCOVERY_FOUND = "FOUND"
DISCOVERY_EMPTY = "EMPTY"
DISCOVERY_ERROR = "ERROR"


@dataclass(frozen=True)
class PlatformDiscoveryStatus:
    platform: str
    status: str
    candidate_count: int = 0
    reason: str = ""


@dataclass(frozen=True)
class DiscoveryOutcome:
    candidates: list[CompetitorCandidate]
    platform_statuses: list[PlatformDiscoveryStatus]
    notes: list[str]


def _platform_label(platform: str) -> str:
    return {
        Platform.YOUTUBE: "YouTube",
        Platform.TIKTOK: "TikTok",
        Platform.INSTAGRAM: "Instagram",
    }.get(str(platform or ""), str(platform or "Platform"))


def _render_platform_status(status: PlatformDiscoveryStatus) -> str:
    line = f"{_platform_label(status.platform)}: {status.status}"
    if status.status == DISCOVERY_FOUND:
        line += f" ({status.candidate_count})"
    if status.reason:
        line += f" — {status.reason}"
    return line


def _pick_platform_seed(
    *,
    platform: str,
    seed: SeedResolution | None,
    linked_accounts: list[SeedResolution] | None,
) -> SeedResolution | None:
    for account in linked_accounts or []:
        if account.platform == platform and account.external_id:
            return account
    if seed and seed.platform == platform and seed.external_id:
        return seed
    return None


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


def _instagram_profile_texts(profile: dict, *, n: int) -> list[str]:
    texts: list[str] = []
    for key in ("latestPosts", "latestIgtvVideos"):
        value = profile.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            text = str(item.get("caption") or item.get("description") or item.get("title") or "").strip()
            if text:
                texts.append(text)
            if len(texts) >= n:
                return texts
    return texts


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
        return _instagram_profile_texts(profiles[0], n=n)

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
    linked_accounts: list[SeedResolution] | None = None,
    max_youtube_search_calls: int,
    max_candidates_per_platform: int,
) -> DiscoveryOutcome:
    candidates: list[CompetitorCandidate] = []
    platform_statuses: list[PlatformDiscoveryStatus] = []

    youtube_seed = _pick_platform_seed(platform=Platform.YOUTUBE, seed=seed, linked_accounts=linked_accounts)
    youtube_competitors = [item for item in competitors if item.platform == Platform.YOUTUBE and item.external_id]
    try:
        youtube_candidates = discover_youtube_competitors(
            keywords=keywords,
            seed=youtube_seed,
            max_search_calls=max_youtube_search_calls,
            max_candidates=max_candidates_per_platform,
            extra_featured_channel_ids=[item.external_id for item in youtube_competitors],
        )
    except Exception as exc:
        platform_statuses.append(
            PlatformDiscoveryStatus(
                platform=Platform.YOUTUBE,
                status=DISCOVERY_ERROR,
                reason=str(exc),
            )
        )
    else:
        candidates.extend(youtube_candidates)
        platform_statuses.append(
            PlatformDiscoveryStatus(
                platform=Platform.YOUTUBE,
                status=DISCOVERY_FOUND if youtube_candidates else DISCOVERY_EMPTY,
                candidate_count=len(youtube_candidates),
                reason="" if youtube_candidates else "по текущим ключевым фразам кандидаты не найдены.",
            )
        )

    instagram_seed = _pick_platform_seed(platform=Platform.INSTAGRAM, seed=seed, linked_accounts=linked_accounts)
    instagram_competitors = [item for item in competitors if item.platform == Platform.INSTAGRAM and item.external_id]
    if instagram_seed:
        try:
            instagram_candidates = discover_instagram_competitors(
                seed=instagram_seed,
                competitors=instagram_competitors,
                max_candidates=max_candidates_per_platform,
            )
        except Exception as exc:
            platform_statuses.append(
                PlatformDiscoveryStatus(
                    platform=Platform.INSTAGRAM,
                    status=DISCOVERY_ERROR,
                    reason=str(exc),
                )
            )
        else:
            if instagram_candidates:
                candidates.extend(instagram_candidates)
                platform_statuses.append(
                    PlatformDiscoveryStatus(
                        platform=Platform.INSTAGRAM,
                        status=DISCOVERY_FOUND,
                        candidate_count=len(instagram_candidates),
                    )
                )
            else:
                platform_statuses.append(
                    PlatformDiscoveryStatus(
                        platform=Platform.INSTAGRAM,
                        status=DISCOVERY_EMPTY,
                        reason="провайдер не вернул связанные профили для автоподбора.",
                    )
                )
    else:
        platform_statuses.append(
            PlatformDiscoveryStatus(
                platform=Platform.INSTAGRAM,
                status=DISCOVERY_EMPTY,
                reason="нет подтвержденного Instagram-профиля для автоподбора.",
            )
        )

    tiktok_seed = _pick_platform_seed(platform=Platform.TIKTOK, seed=seed, linked_accounts=linked_accounts)
    if tiktok_seed:
        platform_statuses.append(
            PlatformDiscoveryStatus(
                platform=Platform.TIKTOK,
                status=DISCOVERY_EMPTY,
                reason="текущий провайдер не отдает связанные профили, поэтому автоподбор пока недоступен.",
            )
        )
    else:
        platform_statuses.append(
            PlatformDiscoveryStatus(
                platform=Platform.TIKTOK,
                status=DISCOVERY_EMPTY,
                reason="нет подтвержденного TikTok-профиля для автоподбора.",
            )
        )

    notes = [_render_platform_status(status) for status in platform_statuses]
    return DiscoveryOutcome(candidates=candidates, platform_statuses=platform_statuses, notes=notes)
