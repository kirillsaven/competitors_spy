from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import re
from typing import Any

from tracking.adapters.base import CompetitorCandidate, SeedResolution
from tracking.adapters.instagram import (
    ApifyInstagramClient,
    InstagramApiError,
    build_profile_url as build_instagram_profile_url,
)
from tracking.adapters.tiktok import (
    ApifyTikTokClient,
    TikTokApiError,
    build_profile_url as build_tiktok_profile_url,
    item_to_video_details,
)
from tracking.adapters.youtube import YouTubeApiError
from tracking.models import Platform
from tracking.services.provider_config import get_instagram_apify_config, get_tiktok_apify_config
from tracking.services.youtube_service import get_recent_video_titles, get_youtube_client


class PlatformOnboardingError(RuntimeError):
    pass


DISCOVERY_FOUND = "FOUND"
DISCOVERY_EMPTY = "EMPTY"
DISCOVERY_ERROR = "ERROR"

_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_GENERIC_QUERY_TOKENS = {
    "account",
    "accounts",
    "channel",
    "creator",
    "official",
    "profile",
    "video",
    "videos",
    "youtube",
    "instagram",
    "tiktok",
    "english",
    "канал",
    "официальный",
    "профиль",
}
_RU_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "ого",
    "его",
    "ему",
    "ому",
    "ыми",
    "ими",
    "иях",
    "ах",
    "ях",
    "ия",
    "ий",
    "ый",
    "ой",
    "ая",
    "ое",
    "ые",
    "ие",
    "ом",
    "ем",
    "ам",
    "ям",
    "ов",
    "ев",
    "ей",
    "ую",
    "юю",
    "ть",
    "ти",
    "ся",
    "сь",
    "а",
    "я",
    "ы",
    "и",
    "е",
    "о",
    "у",
)
_EN_SUFFIXES = (
    "ments",
    "ation",
    "ities",
    "ings",
    "ment",
    "ions",
    "tion",
    "ness",
    "able",
    "ible",
    "ers",
    "ing",
    "ies",
    "ied",
    "est",
    "er",
    "ed",
    "ly",
    "es",
    "s",
)


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


@dataclass
class _DiscoveryCandidate:
    platform: str
    external_id: str
    handle: str | None
    url: str
    display_name: str | None
    description: str = ""
    query_hits: set[str] = field(default_factory=set)
    cross_platform_keys: set[str] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def sort_tiebreak(self) -> tuple[int, str, str]:
        return (
            int(self.metadata.get("rank_hint") or 0),
            str(self.display_name or "").lower(),
            self.external_id,
        )


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


def _normalize_token(token: str) -> str:
    return str(token or "").strip().lower().replace("ё", "е")


def _stem_token(token: str) -> str:
    norm = _normalize_token(token)
    if len(norm) <= 4:
        return norm
    if re.search(r"[а-я]", norm):
        for suffix in _RU_SUFFIXES:
            if len(norm) - len(suffix) >= 4 and norm.endswith(suffix):
                return norm[: -len(suffix)]
        return norm
    for suffix in _EN_SUFFIXES:
        if len(norm) - len(suffix) >= 4 and norm.endswith(suffix):
            return norm[: -len(suffix)]
    return norm


def _token_stems(*values: str | None) -> set[str]:
    stems: set[str] = set()
    for value in values:
        for token in _TOKEN_RE.findall(str(value or "")):
            norm = _normalize_token(token)
            if len(norm) < 3 or norm in _GENERIC_QUERY_TOKENS:
                continue
            stems.add(_stem_token(norm))
    return stems


def _normalize_handle(value: str | None) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", _normalize_token(value or ""))


def _identity_keys(seed: SeedResolution | None, linked_accounts: list[SeedResolution] | None) -> tuple[set[tuple[str, str]], dict[str, set[str]], list[tuple[str, str]]]:
    excluded_ids: set[tuple[str, str]] = set()
    handles_by_platform: dict[str, set[str]] = {
        Platform.YOUTUBE: set(),
        Platform.TIKTOK: set(),
        Platform.INSTAGRAM: set(),
    }
    titles: list[tuple[str, str]] = []
    for account in [seed] + list(linked_accounts or []):
        if not account:
            continue
        if account.external_id:
            excluded_ids.add((account.platform, account.external_id))
        normalized_handle = _normalize_handle(account.handle)
        if normalized_handle:
            handles_by_platform.setdefault(account.platform, set()).add(normalized_handle)
        title = str(account.title or "").strip().lower()
        if title:
            titles.append((account.platform, title))
    return excluded_ids, handles_by_platform, titles


def _candidate_identity_key(candidate: _DiscoveryCandidate) -> str:
    handle = _normalize_handle(candidate.handle)
    if handle:
        return handle
    title_stems = sorted(_token_stems(candidate.display_name))
    return "|".join(title_stems[:3])


def _is_seed_like_candidate(
    candidate: _DiscoveryCandidate,
    *,
    excluded_ids: set[tuple[str, str]],
    handles_by_platform: dict[str, set[str]],
    seed_titles: list[tuple[str, str]],
) -> bool:
    if (candidate.platform, candidate.external_id) in excluded_ids:
        return True

    normalized_handle = _normalize_handle(candidate.handle)
    if normalized_handle and normalized_handle in handles_by_platform.get(candidate.platform, set()):
        return True

    candidate_title = str(candidate.display_name or "").strip().lower()
    if candidate_title:
        for platform, title in seed_titles:
            if platform != candidate.platform:
                continue
            if SequenceMatcher(None, candidate_title, title).ratio() >= 0.95:
                return True
    return False


def _query_stems(query: str) -> set[str]:
    return _token_stems(query)


def _candidate_search_overlap(candidate: _DiscoveryCandidate) -> int:
    candidate_stems = _token_stems(candidate.handle, candidate.display_name, candidate.description)
    overlap = 0
    for query in candidate.query_hits:
        overlap += len(candidate_stems & _query_stems(query))
    return overlap


def _score_candidate(candidate: _DiscoveryCandidate) -> float:
    overlap = _candidate_search_overlap(candidate)
    query_repeat_bonus = len(candidate.query_hits) * 8.0
    overlap_bonus = overlap * 2.4
    description_bonus = 2.4 if len(str(candidate.description or "").strip()) >= 24 else 0.0
    cross_platform_bonus = len(candidate.cross_platform_keys) * 4.5
    verified_bonus = 1.4 if bool(candidate.metadata.get("verified")) else 0.0
    popularity_bonus = min(int(candidate.metadata.get("rank_hint") or 0), 1_000_000) / 250_000
    return query_repeat_bonus + overlap_bonus + description_bonus + cross_platform_bonus + verified_bonus + popularity_bonus


def _search_queries(keywords: list[str], *, max_queries: int = 6) -> list[str]:
    seen: set[str] = set()
    queries: list[str] = []
    for raw in keywords or []:
        query = " ".join(str(raw or "").split()).strip()
        if len(query) < 3:
            continue
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        queries.append(query)
        if len(queries) >= max_queries:
            break
    return queries


def _get_tiktok_client() -> ApifyTikTokClient:
    config = get_tiktok_apify_config()
    profile_actor_id = str(getattr(config, "profile_actor_id", getattr(config, "actor_id", "")) or "")
    search_actor_id = str(getattr(config, "search_actor_id", "") or "")
    if config.provider != "apify":
        raise PlatformOnboardingError(f"Unsupported TikTok provider: {config.provider}")
    if not config.access_token:
        raise PlatformOnboardingError("TIKTOK_PROVIDER_ACCESS_TOKEN is not set")
    if not profile_actor_id:
        raise PlatformOnboardingError("TIKTOK_APIFY_PROFILE_ACTOR_ID is not set")
    if not search_actor_id:
        raise PlatformOnboardingError("TIKTOK_APIFY_SEARCH_ACTOR_ID is not set")
    if not config.base_url:
        raise PlatformOnboardingError("TIKTOK_PROVIDER_BASE_URL is not set")
    return ApifyTikTokClient(
        access_token=config.access_token,
        actor_id=profile_actor_id,
        search_actor_id=search_actor_id,
        base_url=config.base_url,
    )


def _get_instagram_client() -> ApifyInstagramClient:
    config = get_instagram_apify_config()
    profile_actor_id = str(getattr(config, "profile_actor_id", getattr(config, "actor_id", "")) or "")
    search_actor_id = str(getattr(config, "search_actor_id", "") or "")
    if config.provider != "apify":
        raise PlatformOnboardingError(f"Unsupported Instagram provider: {config.provider}")
    if not config.access_token:
        raise PlatformOnboardingError("INSTAGRAM_PROVIDER_ACCESS_TOKEN is not set")
    if not profile_actor_id:
        raise PlatformOnboardingError("INSTAGRAM_APIFY_PROFILE_ACTOR_ID is not set")
    if not search_actor_id:
        raise PlatformOnboardingError("INSTAGRAM_APIFY_SEARCH_ACTOR_ID is not set")
    if not config.base_url:
        raise PlatformOnboardingError("INSTAGRAM_PROVIDER_BASE_URL is not set")
    return ApifyInstagramClient(
        access_token=config.access_token,
        actor_id=profile_actor_id,
        search_actor_id=search_actor_id,
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


def _build_youtube_candidate(item: dict[str, Any], *, queries: set[str]) -> _DiscoveryCandidate | None:
    external_id = str(item.get("id") or "").strip()
    if not external_id:
        return None
    snippet = item.get("snippet") or {}
    statistics = item.get("statistics") or {}
    custom_url = str(snippet.get("customUrl") or "").strip()
    handle = custom_url[1:] if custom_url.startswith("@") else None
    return _DiscoveryCandidate(
        platform=Platform.YOUTUBE,
        external_id=external_id,
        handle=handle,
        url=f"https://www.youtube.com/channel/{external_id}",
        display_name=str(snippet.get("title") or "").strip() or None,
        description=str(snippet.get("description") or "").strip(),
        query_hits=set(queries),
        metadata={"rank_hint": int(statistics.get("subscriberCount") or 0)},
    )


def _discover_youtube_search_candidates(
    *,
    keywords: list[str],
    max_search_calls: int,
) -> list[_DiscoveryCandidate]:
    queries = _search_queries(keywords, max_queries=max_search_calls)
    if not queries:
        raise PlatformOnboardingError("No YouTube search queries could be built from niche keywords")
    client = get_youtube_client()
    try:
        query_hits: dict[str, set[str]] = {}
        for query in queries:
            for channel_id in client.search_channels(q=query, max_results=10):
                query_hits.setdefault(channel_id, set()).add(query)
        if not query_hits:
            return []

        out: list[_DiscoveryCandidate] = []
        channel_ids = list(query_hits)
        for index in range(0, len(channel_ids), 50):
            items = client.channels_list(part="snippet,statistics", ids=channel_ids[index : index + 50])
            for item in items:
                channel_id = str(item.get("id") or "").strip()
                candidate = _build_youtube_candidate(item, queries=query_hits.get(channel_id, set()))
                if candidate:
                    out.append(candidate)
        return out
    finally:
        client.close()


def _build_instagram_candidate(raw: dict[str, Any], *, queries: set[str]) -> _DiscoveryCandidate | None:
    external_id = str(raw.get("id") or "").strip()
    username = str(raw.get("username") or "").strip()
    if not external_id or not username:
        return None
    return _DiscoveryCandidate(
        platform=Platform.INSTAGRAM,
        external_id=external_id,
        handle=username,
        url=str(raw.get("url") or build_instagram_profile_url(username)),
        display_name=str(raw.get("full_name") or raw.get("fullName") or username).strip() or None,
        description=str(raw.get("biography") or "").strip(),
        query_hits=set(queries),
        metadata={"verified": bool(raw.get("is_verified") or raw.get("isVerified"))},
    )


def _search_instagram_candidates_raw(
    *,
    keywords: list[str],
    max_candidates: int = 20,
) -> list[_DiscoveryCandidate]:
    queries = _search_queries(keywords)
    if not queries:
        raise PlatformOnboardingError("No Instagram search queries could be built from niche keywords")
    client = _get_instagram_client()
    try:
        raw_by_id: dict[str, _DiscoveryCandidate] = {}
        for query in queries:
            for raw in client.search_profiles(query=query)[: max(10, max_candidates * 2)]:
                candidate = _build_instagram_candidate(raw, queries={query})
                if not candidate:
                    continue
                existing = raw_by_id.get(candidate.external_id)
                if existing is None:
                    raw_by_id[candidate.external_id] = candidate
                else:
                    existing.query_hits.update(candidate.query_hits)

        if not raw_by_id:
            return []

        ranked = sorted(
            raw_by_id.values(),
            key=lambda item: (-_score_candidate(item), -len(item.query_hits), item.sort_tiebreak),
        )
        return ranked[:max_candidates]
    finally:
        client.close()


def discover_instagram_competitors(
    *,
    keywords: list[str],
    max_candidates: int = 20,
) -> list[CompetitorCandidate]:
    return [
        CompetitorCandidate(
            platform=item.platform,
            external_id=item.external_id,
            handle=item.handle,
            url=item.url,
            display_name=item.display_name,
            reason="search: " + ", ".join(sorted(item.query_hits)),
        )
        for item in _search_instagram_candidates_raw(keywords=keywords, max_candidates=max_candidates)
    ]


def _build_tiktok_candidate(raw: dict[str, Any], *, queries: set[str]) -> _DiscoveryCandidate | None:
    external_id = str(raw.get("id") or "").strip()
    handle = str(raw.get("name") or "").strip()
    if not external_id or not handle:
        return None
    return _DiscoveryCandidate(
        platform=Platform.TIKTOK,
        external_id=external_id,
        handle=handle,
        url=str(raw.get("profileUrl") or build_tiktok_profile_url(handle)),
        display_name=str(raw.get("nickName") or handle).strip() or None,
        description=str(raw.get("signature") or "").strip(),
        query_hits=set(queries),
        metadata={
            "verified": bool(raw.get("verified")),
            "rank_hint": int(raw.get("fans") or raw.get("heart") or 0),
        },
    )


def _search_tiktok_candidates_raw(
    *,
    keywords: list[str],
    max_candidates: int = 20,
) -> list[_DiscoveryCandidate]:
    queries = _search_queries(keywords)
    if not queries:
        raise PlatformOnboardingError("No TikTok search queries could be built from niche keywords")
    client = _get_tiktok_client()
    try:
        raw_by_id: dict[str, _DiscoveryCandidate] = {}
        for query in queries:
            for raw in client.search_profiles(query=query)[: max(10, max_candidates * 2)]:
                candidate = _build_tiktok_candidate(raw, queries={query})
                if not candidate:
                    continue
                existing = raw_by_id.get(candidate.external_id)
                if existing is None:
                    raw_by_id[candidate.external_id] = candidate
                else:
                    existing.query_hits.update(candidate.query_hits)

        ranked = sorted(
            raw_by_id.values(),
            key=lambda item: (-_score_candidate(item), -len(item.query_hits), item.sort_tiebreak),
        )
        return ranked[:max_candidates]
    finally:
        client.close()


def discover_tiktok_competitors(
    *,
    keywords: list[str],
    max_candidates: int = 20,
) -> list[CompetitorCandidate]:
    return [
        CompetitorCandidate(
            platform=item.platform,
            external_id=item.external_id,
            handle=item.handle,
            url=item.url,
            display_name=item.display_name,
            reason="search: " + ", ".join(sorted(item.query_hits)),
        )
        for item in _search_tiktok_candidates_raw(keywords=keywords, max_candidates=max_candidates)
    ]


def _apply_cross_platform_bonus(candidates_by_platform: dict[str, list[_DiscoveryCandidate]]) -> None:
    identity_map: dict[str, set[str]] = {}
    for platform, candidates in candidates_by_platform.items():
        for candidate in candidates:
            identity_key = _candidate_identity_key(candidate)
            if not identity_key:
                continue
            identity_map.setdefault(identity_key, set()).add(platform)

    for platform, candidates in candidates_by_platform.items():
        for candidate in candidates:
            identity_key = _candidate_identity_key(candidate)
            seen_platforms = identity_map.get(identity_key, set())
            if len(seen_platforms) > 1:
                candidate.cross_platform_keys.update(seen_platforms - {platform})


def _filter_and_rank_candidates(
    *,
    candidates: list[_DiscoveryCandidate],
    seed: SeedResolution | None,
    linked_accounts: list[SeedResolution] | None,
    max_candidates: int,
) -> list[CompetitorCandidate]:
    excluded_ids, handles_by_platform, seed_titles = _identity_keys(seed, linked_accounts)
    filtered: list[_DiscoveryCandidate] = []
    for candidate in candidates:
        if _is_seed_like_candidate(
            candidate,
            excluded_ids=excluded_ids,
            handles_by_platform=handles_by_platform,
            seed_titles=seed_titles,
        ):
            continue
        if not candidate.query_hits:
            continue
        filtered.append(candidate)

    ranked = sorted(
        filtered,
        key=lambda item: (-_score_candidate(item), -len(item.query_hits), item.sort_tiebreak),
    )
    return [
        CompetitorCandidate(
            platform=item.platform,
            external_id=item.external_id,
            handle=item.handle,
            url=item.url,
            display_name=item.display_name,
            reason="search: " + ", ".join(sorted(item.query_hits)),
        )
        for item in ranked[:max_candidates]
    ]


def discover_competitors_for_onboarding(
    *,
    keywords: list[str],
    seed: SeedResolution | None,
    competitors: list[SeedResolution],
    linked_accounts: list[SeedResolution] | None = None,
    max_youtube_search_calls: int,
    max_candidates_per_platform: int,
) -> DiscoveryOutcome:
    del competitors  # setup still keeps manual competitors separately; discovery is query-based here.

    platform_statuses: list[PlatformDiscoveryStatus] = []
    candidates_by_platform: dict[str, list[_DiscoveryCandidate]] = {
        Platform.YOUTUBE: [],
        Platform.TIKTOK: [],
        Platform.INSTAGRAM: [],
    }

    try:
        candidates_by_platform[Platform.YOUTUBE] = _discover_youtube_search_candidates(
            keywords=keywords,
            max_search_calls=max_youtube_search_calls,
        )
    except (PlatformOnboardingError, YouTubeApiError, RuntimeError) as exc:
        platform_statuses.append(
            PlatformDiscoveryStatus(platform=Platform.YOUTUBE, status=DISCOVERY_ERROR, reason=str(exc))
        )

    try:
        candidates_by_platform[Platform.INSTAGRAM] = _search_instagram_candidates_raw(
            keywords=keywords,
            max_candidates=max_candidates_per_platform * 2,
        )
    except (PlatformOnboardingError, InstagramApiError, RuntimeError) as exc:
        platform_statuses.append(
            PlatformDiscoveryStatus(platform=Platform.INSTAGRAM, status=DISCOVERY_ERROR, reason=str(exc))
        )

    try:
        candidates_by_platform[Platform.TIKTOK] = _search_tiktok_candidates_raw(
            keywords=keywords,
            max_candidates=max_candidates_per_platform * 2,
        )
    except (PlatformOnboardingError, TikTokApiError, RuntimeError) as exc:
        platform_statuses.append(
            PlatformDiscoveryStatus(platform=Platform.TIKTOK, status=DISCOVERY_ERROR, reason=str(exc))
        )

    _apply_cross_platform_bonus(candidates_by_platform)

    final_candidates: list[CompetitorCandidate] = []
    errored_platforms = {status.platform for status in platform_statuses if status.status == DISCOVERY_ERROR}
    for platform in (Platform.YOUTUBE, Platform.INSTAGRAM, Platform.TIKTOK):
        if platform in errored_platforms:
            continue
        ranked = _filter_and_rank_candidates(
            candidates=candidates_by_platform[platform],
            seed=seed,
            linked_accounts=linked_accounts,
            max_candidates=max_candidates_per_platform,
        )
        final_candidates.extend(ranked)
        platform_statuses.append(
            PlatformDiscoveryStatus(
                platform=platform,
                status=DISCOVERY_FOUND if ranked else DISCOVERY_EMPTY,
                candidate_count=len(ranked),
                reason="" if ranked else "по текущим поисковым фразам поиск был выполнен, но кандидаты не найдены.",
            )
        )

    platform_statuses.sort(key=lambda item: [Platform.YOUTUBE, Platform.INSTAGRAM, Platform.TIKTOK].index(item.platform))
    notes = [_render_platform_status(status) for status in platform_statuses]
    return DiscoveryOutcome(candidates=final_candidates, platform_statuses=platform_statuses, notes=notes)
