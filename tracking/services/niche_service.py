from __future__ import annotations

import logging
import re

from common.text import KeywordSource, extract_keywords

from tracking.adapters.base import SeedResolution
from tracking.services.llm_gemini import GeminiError, infer_keywords_ru
from tracking.services.platform_onboarding import get_recent_seed_content_texts

logger = logging.getLogger(__name__)


def _account_key(seed: SeedResolution) -> str:
    return f"{seed.platform}:{seed.external_id}"


def _ordered_accounts(*, seed: SeedResolution, linked_accounts: list[SeedResolution] | None = None) -> list[SeedResolution]:
    accounts: list[SeedResolution] = []
    seen: set[str] = set()
    for candidate in [seed] + list(linked_accounts or []):
        if not candidate or not candidate.external_id:
            continue
        key = _account_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        accounts.append(candidate)
    return accounts


def _append_unique(parts: list[str], value: str, seen: set[str]) -> None:
    text = str(value or "").strip()
    if not text:
        return
    key = text.lower()
    if key in seen:
        return
    seen.add(key)
    parts.append(text)


def _append_keyword_source(
    sources: list[KeywordSource],
    *,
    text: str,
    source_id: str,
    source_type: str,
    seen: set[tuple[str, str]],
) -> None:
    value = str(text or "").strip()
    if not value:
        return
    key = (source_id, value.lower())
    if key in seen:
        return
    seen.add(key)
    sources.append(KeywordSource(text=value, source_id=source_id, source_type=source_type))


def _safe_recent_seed_content_texts(*, seed: SeedResolution, n: int = 10) -> list[str]:
    try:
        return get_recent_seed_content_texts(seed=seed, n=n)
    except Exception as exc:
        logger.warning("Keyword source fetch failed for %s:%s: %s", seed.platform, seed.external_id, exc)
        return []


def build_niche_context_text(
    *,
    seed: SeedResolution,
    competitors: list[SeedResolution],
    linked_accounts: list[SeedResolution] | None = None,
) -> str:
    lines: list[str] = []
    accounts = _ordered_accounts(seed=seed, linked_accounts=linked_accounts)
    lines.append("CONFIRMED USER ACCOUNTS" if len(accounts) > 1 else "SEED CHANNEL")
    for index, account in enumerate(accounts):
        if index > 0:
            lines.append("")
        lines.append(f"Platform: {account.platform}")
        if account.handle:
            lines.append(f"Handle: {account.handle}")
        if account.url:
            lines.append(f"URL: {account.url}")
        if account.title:
            lines.append(f"Title: {account.title}")
        if account.description:
            lines.append(f"Description: {account.description}")

        recent = _safe_recent_seed_content_texts(seed=account, n=10)
        if recent:
            lines.append("Recent content:")
            for text in recent:
                lines.append(f"- {text}")

    if competitors:
        lines.append("")
        lines.append("USER-PROVIDED COMPETITORS")
        for c in competitors[:20]:
            name = c.title or c.handle or c.external_id
            lines.append(f"Channel: {name}")
            if c.handle:
                lines.append(f"Handle: {c.handle}")
            if c.url:
                lines.append(f"URL: {c.url}")
            if c.description:
                lines.append(f"Description: {c.description}")

    return "\n".join(lines).strip()


def build_keyword_source_text(
    *,
    seed: SeedResolution,
    competitors: list[SeedResolution],
    linked_accounts: list[SeedResolution] | None = None,
) -> str:
    parts: list[str] = []
    seen_parts: set[str] = set()
    for account in _ordered_accounts(seed=seed, linked_accounts=linked_accounts):
        if account.title:
            _append_unique(parts, account.title, seen_parts)
        if account.description:
            _append_unique(parts, account.description, seen_parts)
        for text in _safe_recent_seed_content_texts(seed=account, n=10):
            _append_unique(parts, text, seen_parts)

    for competitor in competitors[:20]:
        if competitor.description:
            _append_unique(parts, competitor.description, seen_parts)

    return "\n".join(part for part in parts if part).strip()


def build_keyword_sources(
    *,
    seed: SeedResolution,
    competitors: list[SeedResolution],
    linked_accounts: list[SeedResolution] | None = None,
) -> list[KeywordSource]:
    sources: list[KeywordSource] = []
    seen_sources: set[tuple[str, str]] = set()
    for account in _ordered_accounts(seed=seed, linked_accounts=linked_accounts):
        source_id = _account_key(account)
        if account.title:
            _append_keyword_source(
                sources,
                text=account.title,
                source_id=source_id,
                source_type="title",
                seen=seen_sources,
            )
        if account.description:
            _append_keyword_source(
                sources,
                text=account.description,
                source_id=source_id,
                source_type="description",
                seen=seen_sources,
            )
        for text in _safe_recent_seed_content_texts(seed=account, n=10):
            _append_keyword_source(
                sources,
                text=text,
                source_id=source_id,
                source_type="recent",
                seen=seen_sources,
            )

    for competitor in competitors[:20]:
        if competitor.description and competitor.external_id:
            _append_keyword_source(
                sources,
                text=competitor.description,
                source_id=f"{competitor.platform}:{competitor.external_id}",
                source_type="competitor",
                seen=seen_sources,
            )

    return sources


def build_keyword_identity_terms(
    *,
    seed: SeedResolution,
    linked_accounts: list[SeedResolution] | None = None,
) -> set[str]:
    identity_terms: set[str] = set()
    ordered_accounts = _ordered_accounts(seed=seed, linked_accounts=linked_accounts)
    repeated_titles: dict[str, int] = {}
    for account in ordered_accounts:
        title = " ".join(str(account.title or "").strip().split())
        if title:
            repeated_titles[title] = repeated_titles.get(title, 0) + 1

    for account in ordered_accounts:
        handle = str(account.handle or "").strip()
        if handle:
            identity_terms.add(handle)

        title = str(account.title or "").strip()
        if not title:
            continue
        normalized_title = " ".join(title.split())
        if repeated_titles.get(normalized_title, 0) > 1 and 1 <= len(normalized_title.split()) <= 4:
            identity_terms.add(normalized_title)
        parts = [segment.strip() for segment in re.split(r"[|/•]+", title) if segment.strip()]
        if len(parts) < 2:
            continue
        first_segment = parts[0]
        if 1 <= len(first_segment.split()) <= 4:
            identity_terms.add(first_segment)
    return identity_terms


def infer_niche_keywords(
    *,
    seed: SeedResolution,
    competitors: list[SeedResolution],
    prefer_llm: bool,
    linked_accounts: list[SeedResolution] | None = None,
) -> tuple[list[str], str]:
    """
    Returns: (keywords, source) where source in {"llm","auto"}.
    """
    context = build_niche_context_text(seed=seed, competitors=competitors, linked_accounts=linked_accounts)
    keyword_sources = build_keyword_sources(seed=seed, competitors=competitors, linked_accounts=linked_accounts)
    auto_keywords = extract_keywords(
        keyword_sources,
        max_keywords=8,
        identity_terms=build_keyword_identity_terms(
            seed=seed,
            linked_accounts=linked_accounts,
        ),
    )
    if len(auto_keywords) >= 3 or not prefer_llm:
        return auto_keywords, "auto"

    if prefer_llm:
        try:
            kws = infer_keywords_ru(context_text=context)
            if kws:
                return kws, "llm"
        except GeminiError as e:
            logger.warning("LLM niche inference failed: %s", e)
        except Exception as e:
            logger.exception("Unexpected LLM niche inference error: %s", e)

    return auto_keywords, "auto"
