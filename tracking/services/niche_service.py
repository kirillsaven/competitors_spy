from __future__ import annotations

import logging

from common.text import extract_keywords

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

        recent = get_recent_seed_content_texts(seed=account, n=10)
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
        if account.description:
            _append_unique(parts, account.description, seen_parts)
        for text in get_recent_seed_content_texts(seed=account, n=10):
            _append_unique(parts, text, seen_parts)

    for competitor in competitors[:20]:
        if competitor.description:
            _append_unique(parts, competitor.description, seen_parts)

    return "\n".join(part for part in parts if part).strip()


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

    if prefer_llm:
        try:
            kws = infer_keywords_ru(context_text=context)
            if kws:
                return kws, "llm"
        except GeminiError as e:
            logger.warning("LLM niche inference failed: %s", e)
        except Exception as e:
            logger.exception("Unexpected LLM niche inference error: %s", e)

    kws = extract_keywords(
        build_keyword_source_text(seed=seed, competitors=competitors, linked_accounts=linked_accounts),
        max_keywords=8,
    )
    return kws, "auto"
