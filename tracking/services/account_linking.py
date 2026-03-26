from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from urllib.parse import urlparse

from tracking.adapters.base import SeedResolution
from tracking.adapters.instagram import (
    ApifyInstagramClient,
    extract_handle as extract_instagram_handle,
    seed_from_profile,
)
from tracking.adapters.tiktok import (
    ApifyTikTokClient,
    extract_handle as extract_tiktok_handle,
    seed_from_item,
)
from tracking.adapters.youtube import extract_handle as extract_youtube_handle
from tracking.models import Platform, TgUser, UserLinkedAccount
from tracking.services.platform_onboarding import fetch_instagram_profiles_cached, fetch_tiktok_profile_feed_cached
from tracking.services.provider_config import get_instagram_apify_config, get_tiktok_apify_config
from tracking.services.seed_resolver import SeedResolveError, resolve_seed_for_platform
from tracking.services.setup_runtime import (
    PLATFORM_STATE_ERROR,
    PLATFORM_STATE_UNAVAILABLE,
    SetupRunContext,
    get_platform_state,
    mark_platform_failure,
    platform_is_blocked,
)
from tracking.services.youtube_service import search_youtube_seed_candidates


_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_URL_RE = re.compile(r"https?://[^\s)]+", re.IGNORECASE)
_BARE_DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.IGNORECASE)
_IGNORED_DOMAINS = {
    "youtube.com",
    "www.youtube.com",
    "youtu.be",
    "tiktok.com",
    "www.tiktok.com",
    "instagram.com",
    "www.instagram.com",
}
_MAX_HANDLE_CANDIDATES = 4
_MAX_YOUTUBE_SEARCH_CANDIDATES = 5
_MIN_MATCH_SCORE_CROSS_PLATFORM = 45
_MIN_MATCH_SCORE_YOUTUBE = 25


@dataclass(frozen=True)
class LinkedAccountCandidate:
    seed: SeedResolution
    signals: list[str]
    score: int


@dataclass(frozen=True)
class LinkedAccountSuggestion:
    platform: str
    candidates: list[LinkedAccountCandidate]
    note: str | None = None
    status: str = "AVAILABLE"


@dataclass(frozen=True)
class CandidateProfile:
    seed: SeedResolution
    description: str
    external_domains: set[str]
    provider_hints: set[str]


def _get_tiktok_client() -> ApifyTikTokClient:
    config = get_tiktok_apify_config()
    profile_actor_id = str(getattr(config, "profile_actor_id", getattr(config, "actor_id", "")) or "")
    search_actor_id = str(getattr(config, "search_actor_id", "") or "")
    if config.provider != "apify":
        raise SeedResolveError(f"Unsupported TikTok provider: {config.provider}")
    if not config.access_token:
        raise SeedResolveError("TIKTOK_PROVIDER_ACCESS_TOKEN is not set")
    if not profile_actor_id:
        raise SeedResolveError("TIKTOK_APIFY_PROFILE_ACTOR_ID is not set")
    if not config.base_url:
        raise SeedResolveError("TIKTOK_PROVIDER_BASE_URL is not set")
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
        raise SeedResolveError(f"Unsupported Instagram provider: {config.provider}")
    if not config.access_token:
        raise SeedResolveError("INSTAGRAM_PROVIDER_ACCESS_TOKEN is not set")
    if not profile_actor_id:
        raise SeedResolveError("INSTAGRAM_APIFY_PROFILE_ACTOR_ID is not set")
    if not config.base_url:
        raise SeedResolveError("INSTAGRAM_PROVIDER_BASE_URL is not set")
    return ApifyInstagramClient(
        access_token=config.access_token,
        actor_id=profile_actor_id,
        search_actor_id=search_actor_id,
        base_url=config.base_url,
    )


def _normalize_handle(value: str | None) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", str(value or "").strip().lower())


def _name_tokens(*values: str | None) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        for token in _TOKEN_RE.findall(str(value or "").lower()):
            if len(token) >= 3:
                tokens.add(token)
    return tokens


def _display_similarity(left: str | None, right: str | None) -> float:
    return SequenceMatcher(None, str(left or "").strip().lower(), str(right or "").strip().lower()).ratio()


def _extract_urls(*values: str | None) -> set[str]:
    urls: set[str] = set()
    for value in values:
        for url in _URL_RE.findall(str(value or "")):
            urls.add(url.rstrip(".,);]"))
    return urls


def _extract_domains(*values: str | None) -> set[str]:
    domains: set[str] = set()
    for value in values:
        text = str(value or "")
        for url in _URL_RE.findall(text):
            host = (urlparse(url).netloc or "").lower().strip(".")
            if host and host not in _IGNORED_DOMAINS:
                domains.add(host)
        for raw_domain in _BARE_DOMAIN_RE.findall(text):
            host = raw_domain.lower().strip(".")
            if host and host not in _IGNORED_DOMAINS:
                domains.add(host)
    return domains


def _extract_platform_handles_from_urls(platform: str, *values: str | None) -> set[str]:
    handles: set[str] = set()
    for url in _extract_urls(*values):
        if platform == Platform.INSTAGRAM:
            handle = extract_instagram_handle(url)
        elif platform == Platform.TIKTOK:
            handle = extract_tiktok_handle(url)
        elif platform == Platform.YOUTUBE:
            handle = extract_youtube_handle(url)
        else:
            handle = None
        if handle:
            handles.add(handle)
    return handles


def _candidate_key(seed: SeedResolution) -> str:
    return f"{seed.platform}:{seed.external_id}"


def _build_seed_hints(seed: SeedResolution) -> dict[str, set[str]]:
    return {
        Platform.INSTAGRAM: _extract_platform_handles_from_urls(Platform.INSTAGRAM, seed.url, seed.description, seed.title),
        Platform.TIKTOK: _extract_platform_handles_from_urls(Platform.TIKTOK, seed.url, seed.description, seed.title),
        Platform.YOUTUBE: _extract_platform_handles_from_urls(Platform.YOUTUBE, seed.url, seed.description, seed.title),
    }


def _score_candidate(
    *,
    source_seed: SeedResolution,
    target_platform: str,
    candidate: CandidateProfile,
    source_domains: set[str],
    source_tokens: set[str],
    explicit_hints: set[str],
) -> LinkedAccountCandidate | None:
    score = 0
    signals: list[str] = []

    source_handle = str(source_seed.handle or "").strip()
    candidate_handle = str(candidate.seed.handle or "").strip()
    if source_handle and candidate_handle and source_handle.lower() == candidate_handle.lower():
        signals.append("exact_handle")
        score += 100

    normalized_source = _normalize_handle(source_handle)
    normalized_candidate = _normalize_handle(candidate_handle)
    if (
        normalized_source
        and normalized_candidate
        and normalized_source == normalized_candidate
        and source_handle.lower() != candidate_handle.lower()
    ):
        signals.append("normalized_handle")
        score += 60

    similarity = _display_similarity(source_seed.title, candidate.seed.title)
    if similarity >= 0.92:
        signals.append(f"display_similarity:{similarity:.2f}")
        score += 40
    elif similarity >= 0.75:
        signals.append(f"display_similarity:{similarity:.2f}")
        score += 25

    shared_tokens = sorted(source_tokens & _name_tokens(candidate.seed.title, candidate.seed.handle, candidate.description))
    if shared_tokens:
        signals.append(f"shared_tokens:{','.join(shared_tokens[:3])}")
        score += min(len(shared_tokens), 4) * 8

    shared_domains = sorted(source_domains & candidate.external_domains)
    if shared_domains:
        signals.append(f"shared_domains:{','.join(shared_domains[:2])}")
        score += min(len(shared_domains), 2) * 25

    hinted = sorted(explicit_hints & {_normalize_handle(candidate_handle), candidate_handle.lower()})
    if hinted:
        signals.append("provider_hint")
        score += 35

    provider_mentions = candidate.provider_hints
    if source_handle and _normalize_handle(source_handle) in provider_mentions:
        signals.append("provider_hint")
        score += 20

    unique_signals: list[str] = []
    seen_signals: set[str] = set()
    for signal in signals:
        if signal in seen_signals:
            continue
        seen_signals.add(signal)
        unique_signals.append(signal)

    threshold = (
        _MIN_MATCH_SCORE_CROSS_PLATFORM
        if target_platform in {Platform.TIKTOK, Platform.INSTAGRAM}
        else _MIN_MATCH_SCORE_YOUTUBE
    )
    if score < threshold:
        return None
    return LinkedAccountCandidate(seed=candidate.seed, signals=unique_signals, score=score)


def _candidate_handles_for_platform(seed: SeedResolution, target_platform: str) -> list[str]:
    values: list[str] = []
    seed_handle = str(seed.handle or "").strip()
    if seed_handle:
        values.append(seed_handle)
    normalized = _normalize_handle(seed_handle)
    if normalized and normalized != seed_handle.lower():
        values.append(normalized)
    for hinted in sorted(_build_seed_hints(seed).get(target_platform, set())):
        if hinted and hinted not in values:
            values.append(hinted)
    return values[:_MAX_HANDLE_CANDIDATES]


def _build_candidate_profile_from_instagram(profile: dict) -> CandidateProfile | None:
    seed = seed_from_profile(profile)
    if not seed:
        return None
    description = str(profile.get("biography") or seed.description or "").strip()
    raw_link_values = [
        profile.get("externalUrl"),
        profile.get("url"),
    ]
    for key in ("externalUrls", "bioLinks", "links"):
        value = profile.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    raw_link_values.extend(item.values())
                else:
                    raw_link_values.append(item)
    domains = _extract_domains(description, *[str(item or "") for item in raw_link_values])
    hints = {_normalize_handle(handle) for handle in _extract_platform_handles_from_urls(Platform.TIKTOK, description, *raw_link_values)}
    hints |= {_normalize_handle(handle) for handle in _extract_platform_handles_from_urls(Platform.YOUTUBE, description, *raw_link_values)}
    hints |= {_normalize_handle(handle) for handle in _extract_platform_handles_from_urls(Platform.INSTAGRAM, description, *raw_link_values)}
    return CandidateProfile(seed=seed, description=description, external_domains=domains, provider_hints=hints)


def _build_candidate_profiles_from_tiktok(items: list[dict]) -> list[CandidateProfile]:
    by_key: dict[str, CandidateProfile] = {}
    for item in items:
        seed = seed_from_item(item)
        if not seed:
            continue
        author_meta = item.get("authorMeta") or {}
        description = str(author_meta.get("signature") or seed.description or "").strip()
        domains = _extract_domains(description)
        hints = {_normalize_handle(handle) for handle in _extract_platform_handles_from_urls(Platform.INSTAGRAM, description)}
        hints |= {_normalize_handle(handle) for handle in _extract_platform_handles_from_urls(Platform.YOUTUBE, description)}
        hints |= {_normalize_handle(handle) for handle in _extract_platform_handles_from_urls(Platform.TIKTOK, description)}
        key = _candidate_key(seed)
        if key not in by_key:
            by_key[key] = CandidateProfile(seed=seed, description=description, external_domains=domains, provider_hints=hints)
    return list(by_key.values())


def _build_candidate_profile_from_seed(seed: SeedResolution) -> CandidateProfile:
    description = str(seed.description or "").strip()
    return CandidateProfile(
        seed=seed,
        description=description,
        external_domains=_extract_domains(seed.url, description),
        provider_hints={
            _normalize_handle(handle)
            for platform in (Platform.INSTAGRAM, Platform.TIKTOK, Platform.YOUTUBE)
            for handle in _extract_platform_handles_from_urls(platform, seed.url, description)
        },
    )


class CheapAccountMatcher:
    def __init__(
        self,
        *,
        seed: SeedResolution,
        max_candidates: int = 3,
        context: SetupRunContext | None = None,
    ) -> None:
        self.seed = seed
        self.max_candidates = max_candidates
        self.context = context
        self._suggestions: dict[str, LinkedAccountSuggestion] = {}
        self._source_domains = _extract_domains(seed.url, seed.description)
        self._source_tokens = _name_tokens(seed.title, seed.handle, seed.description)
        self._explicit_hints = {
            platform: {_normalize_handle(handle) for handle in handles}
            for platform, handles in _build_seed_hints(seed).items()
        }

    def suggest_for_platform(self, target_platform: str) -> LinkedAccountSuggestion:
        if target_platform in self._suggestions:
            return self._suggestions[target_platform]

        if not target_platform or target_platform == self.seed.platform:
            suggestion = LinkedAccountSuggestion(platform=target_platform, candidates=[], note=None)
        elif target_platform == Platform.INSTAGRAM:
            suggestion = self._suggest_instagram()
        elif target_platform == Platform.TIKTOK:
            suggestion = self._suggest_tiktok()
        elif target_platform == Platform.YOUTUBE:
            suggestion = self._suggest_youtube()
        else:
            suggestion = LinkedAccountSuggestion(platform=target_platform, candidates=[], note=f"Unsupported platform: {target_platform}")
        self._suggestions[target_platform] = suggestion
        return suggestion

    def suggest_for_platforms(self, target_platforms: list[str]) -> dict[str, LinkedAccountSuggestion]:
        return {platform: self.suggest_for_platform(platform) for platform in target_platforms}

    def _rank_profiles(self, target_platform: str, profiles: list[CandidateProfile], note: str | None = None) -> LinkedAccountSuggestion:
        by_key: dict[str, LinkedAccountCandidate] = {}
        for profile in profiles:
            ranked = _score_candidate(
                source_seed=self.seed,
                target_platform=target_platform,
                candidate=profile,
                source_domains=self._source_domains,
                source_tokens=self._source_tokens,
                explicit_hints=self._explicit_hints.get(target_platform, set()),
            )
            if ranked is None:
                continue
            key = _candidate_key(ranked.seed)
            existing = by_key.get(key)
            if existing is None or ranked.score > existing.score:
                by_key[key] = ranked
        candidates = sorted(by_key.values(), key=lambda item: (-item.score, item.seed.title or "", item.seed.external_id))
        return LinkedAccountSuggestion(
            platform=target_platform,
            candidates=candidates[: self.max_candidates],
            note=note if not candidates else None,
        )

    def _suggest_instagram(self) -> LinkedAccountSuggestion:
        if platform_is_blocked(self.context, Platform.INSTAGRAM):
            state = get_platform_state(self.context, Platform.INSTAGRAM)
            return LinkedAccountSuggestion(
                platform=Platform.INSTAGRAM,
                candidates=[],
                note=state.reason or "Instagram временно недоступен для автоподтверждения.",
                status=state.state,
            )
        handles = _candidate_handles_for_platform(self.seed, Platform.INSTAGRAM)
        if not handles:
            return LinkedAccountSuggestion(
                platform=Platform.INSTAGRAM,
                candidates=[],
                note="Нет дешевых кандидатов для Instagram: нет хендла или явных Instagram-подсказок в профиле.",
            )
        try:
            profiles = fetch_instagram_profiles_cached(inputs=handles, context=self.context)
        except Exception as exc:
            status = mark_platform_failure(self.context, platform=Platform.INSTAGRAM, reason=str(exc))
            return LinkedAccountSuggestion(
                platform=Platform.INSTAGRAM,
                candidates=[],
                note=str(exc),
                status=status,
            )
        candidates = [profile for raw in profiles if (profile := _build_candidate_profile_from_instagram(raw)) is not None]
        return self._rank_profiles(Platform.INSTAGRAM, candidates, note="Instagram-провайдер не подтвердил дешевые кандидаты.")

    def _suggest_tiktok(self) -> LinkedAccountSuggestion:
        if platform_is_blocked(self.context, Platform.TIKTOK):
            state = get_platform_state(self.context, Platform.TIKTOK)
            return LinkedAccountSuggestion(
                platform=Platform.TIKTOK,
                candidates=[],
                note=state.reason or "TikTok временно недоступен для автоподтверждения.",
                status=state.state,
            )
        handles = _candidate_handles_for_platform(self.seed, Platform.TIKTOK)
        if not handles:
            return LinkedAccountSuggestion(
                platform=Platform.TIKTOK,
                candidates=[],
                note="Нет дешевых кандидатов для TikTok: нет хендла или явных TikTok-подсказок в профиле.",
            )
        try:
            items: list[dict] = []
            for handle in handles:
                items.extend(
                    fetch_tiktok_profile_feed_cached(handle=handle, results_per_page=1, context=self.context)
                )
        except Exception as exc:
            status = mark_platform_failure(self.context, platform=Platform.TIKTOK, reason=str(exc))
            return LinkedAccountSuggestion(
                platform=Platform.TIKTOK,
                candidates=[],
                note=str(exc),
                status=status,
            )
        profiles = _build_candidate_profiles_from_tiktok(items)
        return self._rank_profiles(Platform.TIKTOK, profiles, note="TikTok-провайдер не подтвердил дешевые кандидаты.")

    def _suggest_youtube(self) -> LinkedAccountSuggestion:
        by_key: dict[str, CandidateProfile] = {}
        notes: list[str] = []

        handle_candidates = _candidate_handles_for_platform(self.seed, Platform.YOUTUBE)
        for handle in handle_candidates[:2]:
            try:
                resolved = resolve_seed_for_platform(platform=Platform.YOUTUBE, raw_input=handle)
            except SeedResolveError as exc:
                notes.append(str(exc))
                continue
            if resolved:
                by_key[_candidate_key(resolved)] = _build_candidate_profile_from_seed(resolved)

        query = str(self.seed.title or self.seed.handle or "").strip()
        if query:
            try:
                found = search_youtube_seed_candidates(
                    query=query,
                    max_results=max(_MAX_YOUTUBE_SEARCH_CANDIDATES, self.max_candidates * 2),
                )
            except Exception as exc:
                notes.append(str(exc))
            else:
                for candidate in found:
                    by_key.setdefault(_candidate_key(candidate), _build_candidate_profile_from_seed(candidate))

        suggestion = self._rank_profiles(
            Platform.YOUTUBE,
            list(by_key.values()),
            note=notes[0] if notes else "По текущим дешевым сигналам не нашел подтвержденный YouTube-канал.",
        )
        return suggestion


def suggest_accounts_for_platform(
    *,
    seed: SeedResolution,
    target_platform: str,
    max_candidates: int = 3,
    context: SetupRunContext | None = None,
) -> LinkedAccountSuggestion:
    matcher = CheapAccountMatcher(seed=seed, max_candidates=max_candidates, context=context)
    return matcher.suggest_for_platform(target_platform)


def suggest_accounts_for_platforms(
    *,
    seed: SeedResolution,
    target_platforms: list[str],
    max_candidates: int = 3,
    context: SetupRunContext | None = None,
) -> dict[str, LinkedAccountSuggestion]:
    matcher = CheapAccountMatcher(seed=seed, max_candidates=max_candidates, context=context)
    return matcher.suggest_for_platforms(target_platforms)


def replace_user_linked_accounts(*, user: TgUser, accounts_by_platform: dict[str, dict]) -> None:
    UserLinkedAccount.objects.filter(user=user).delete()
    rows: list[UserLinkedAccount] = []
    for platform, account in accounts_by_platform.items():
        external_id = str(account.get("external_id") or "").strip()
        if not platform or not external_id:
            continue
        rows.append(
            UserLinkedAccount(
                user=user,
                platform=platform,
                external_id=external_id,
                handle=str(account.get("handle") or "").strip(),
                url=str(account.get("url") or "").strip(),
                display_name=str(account.get("title") or account.get("display_name") or "").strip(),
                source=str(account.get("source") or "manual"),
                is_seed=bool(account.get("is_seed")),
                match_signals=[str(item) for item in (account.get("signals") or []) if str(item).strip()],
                meta=account.get("meta") if isinstance(account.get("meta"), dict) else {},
            )
        )
    if rows:
        UserLinkedAccount.objects.bulk_create(rows)
