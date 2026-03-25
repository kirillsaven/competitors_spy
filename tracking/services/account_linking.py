from __future__ import annotations

from dataclasses import dataclass
import re

from tracking.adapters.base import SeedResolution
from tracking.models import TgUser, UserLinkedAccount
from tracking.services.seed_resolver import SeedResolveError, resolve_seed_for_platform
from tracking.services.youtube_service import search_youtube_seed_candidates


_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")


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


def _name_tokens(*values: str | None) -> set[str]:
    out: set[str] = set()
    for value in values:
        for token in _TOKEN_RE.findall(str(value or "").lower()):
            if len(token) >= 3:
                out.add(token)
    return out


def _shared_name_tokens(seed: SeedResolution, candidate: SeedResolution) -> list[str]:
    return sorted(
        _name_tokens(seed.title, seed.handle, seed.description)
        & _name_tokens(candidate.title, candidate.handle, candidate.description)
    )


def _seed_key(seed: SeedResolution) -> str:
    return f"{seed.platform}:{seed.external_id}"


def _score_youtube_candidate(seed: SeedResolution, candidate: SeedResolution) -> tuple[int, list[str]]:
    signals: list[str] = []
    score = 0

    seed_handle = str(seed.handle or "").strip().lower()
    candidate_handle = str(candidate.handle or "").strip().lower()
    if seed_handle and candidate_handle and seed_handle == candidate_handle:
        signals.append("exact_handle")
        score += 100

    shared_tokens = _shared_name_tokens(seed, candidate)
    if shared_tokens:
        signals.append(f"shared_name_tokens:{','.join(shared_tokens[:3])}")
        score += min(len(shared_tokens), 4) * 10

    seed_title = str(seed.title or "").strip().lower()
    candidate_title = str(candidate.title or "").strip().lower()
    if seed_title and candidate_title and seed_title == candidate_title:
        signals.append("exact_display_name")
        score += 50

    return score, signals


def suggest_accounts_for_platform(
    *,
    seed: SeedResolution,
    target_platform: str,
    max_candidates: int = 3,
) -> LinkedAccountSuggestion:
    if not target_platform or target_platform == seed.platform:
        return LinkedAccountSuggestion(platform=target_platform, candidates=[], note=None)

    if target_platform in {"tiktok", "instagram"}:
        handle = str(seed.handle or "").strip()
        if not handle:
            return LinkedAccountSuggestion(
                platform=target_platform,
                candidates=[],
                note="У исходного профиля нет подтвержденного хендла, поэтому автопоиск по совпадающему аккаунту недоступен.",
            )
        try:
            candidate = resolve_seed_for_platform(platform=target_platform, raw_input=handle)
        except SeedResolveError as exc:
            return LinkedAccountSuggestion(platform=target_platform, candidates=[], note=str(exc))
        if not candidate:
            return LinkedAccountSuggestion(
                platform=target_platform,
                candidates=[],
                note="Профиль с таким же хендлом не подтвердился у провайдера.",
            )
        signals = ["exact_handle"]
        shared_tokens = _shared_name_tokens(seed, candidate)
        if shared_tokens:
            signals.append(f"shared_name_tokens:{','.join(shared_tokens[:3])}")
        return LinkedAccountSuggestion(
            platform=target_platform,
            candidates=[LinkedAccountCandidate(seed=candidate, signals=signals, score=100)],
            note=None,
        )

    if target_platform == "youtube":
        by_key: dict[str, LinkedAccountCandidate] = {}
        notes: list[str] = []
        handle = str(seed.handle or "").strip()
        if handle:
            try:
                exact = resolve_seed_for_platform(platform=target_platform, raw_input=handle)
            except SeedResolveError as exc:
                notes.append(str(exc))
            else:
                if exact:
                    by_key[_seed_key(exact)] = LinkedAccountCandidate(seed=exact, signals=["exact_handle"], score=100)

        query = str(seed.title or "").strip()
        if query:
            try:
                found = search_youtube_seed_candidates(query=query, max_results=max(4, max_candidates * 2))
            except Exception as exc:
                notes.append(str(exc))
            else:
                for candidate in found:
                    score, signals = _score_youtube_candidate(seed, candidate)
                    if score <= 0:
                        continue
                    key = _seed_key(candidate)
                    existing = by_key.get(key)
                    linked = LinkedAccountCandidate(seed=candidate, signals=signals, score=score)
                    if existing is None or linked.score > existing.score:
                        by_key[key] = linked

        candidates = sorted(by_key.values(), key=lambda item: (-item.score, item.seed.title or "", item.seed.external_id))
        note = None
        if not candidates:
            note = notes[0] if notes else "По текущим сигналам не нашел подтвержденный YouTube-канал."
        return LinkedAccountSuggestion(platform=target_platform, candidates=candidates[:max_candidates], note=note)

    return LinkedAccountSuggestion(
        platform=target_platform,
        candidates=[],
        note=f"Unsupported platform: {target_platform}",
    )


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
