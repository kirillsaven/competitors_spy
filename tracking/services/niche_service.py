from __future__ import annotations

import logging
import re

from common.text import KeywordSource, _USEFUL_THEME_STEMS, _stem_token, extract_keywords

from tracking.adapters.base import SeedResolution
from tracking.services.llm_gemini import GeminiError, infer_keywords_ru
from tracking.services.platform_onboarding import get_recent_seed_content_texts
from tracking.services.setup_runtime import SetupRunContext

logger = logging.getLogger(__name__)
_IDENTITY_SAFE_THEME_STEMS = set(_USEFUL_THEME_STEMS) | {"ege", "exam", "егэ", "огэ", "экзам"}
_EXAM_SUBJECT_RE = re.compile(r"\b(егэ|огэ)\s+по\s+([0-9a-zа-яё-]{3,})", flags=re.IGNORECASE)


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


def _safe_recent_seed_content_texts(
    *,
    seed: SeedResolution,
    n: int = 10,
    context: SetupRunContext | None = None,
) -> list[str]:
    try:
        return get_recent_seed_content_texts(seed=seed, n=n, context=context)
    except TypeError:
        return get_recent_seed_content_texts(seed=seed, n=n)
    except Exception as exc:
        logger.warning("Keyword source fetch failed for %s:%s: %s", seed.platform, seed.external_id, exc)
        return []


def build_niche_context_text(
    *,
    seed: SeedResolution,
    competitors: list[SeedResolution],
    linked_accounts: list[SeedResolution] | None = None,
    context: SetupRunContext | None = None,
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

        recent = _safe_recent_seed_content_texts(seed=account, n=10, context=context)
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
    context: SetupRunContext | None = None,
) -> str:
    parts: list[str] = []
    seen_parts: set[str] = set()
    for account in _ordered_accounts(seed=seed, linked_accounts=linked_accounts):
        if account.description:
            _append_unique(parts, account.description, seen_parts)
        for text in _safe_recent_seed_content_texts(seed=account, n=10, context=context):
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
    context: SetupRunContext | None = None,
) -> list[KeywordSource]:
    sources: list[KeywordSource] = []
    seen_sources: set[tuple[str, str]] = set()
    for account in _ordered_accounts(seed=seed, linked_accounts=linked_accounts):
        source_id = _account_key(account)
        if account.description:
            _append_keyword_source(
                sources,
                text=account.description,
                source_id=source_id,
                source_type="description",
                seen=seen_sources,
            )
        for text in _safe_recent_seed_content_texts(seed=account, n=10, context=context):
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


def build_keyword_blocked_terms(
    *,
    seed: SeedResolution,
    linked_accounts: list[SeedResolution] | None = None,
    keyword_sources: list[KeywordSource] | None = None,
) -> set[str]:
    blocked: set[str] = set()
    supporting_tokens: set[str] = set()
    supporting_counts: dict[str, int] = {}
    for source in keyword_sources or []:
        seen_in_source: set[str] = set()
        for token in re.findall(r"[0-9a-zа-яё]+", str(source.text or ""), flags=re.IGNORECASE):
            norm = token.strip().lower().replace("ё", "е")
            if len(norm) >= 3:
                supporting_tokens.add(norm)
                if norm not in seen_in_source:
                    supporting_counts[norm] = supporting_counts.get(norm, 0) + 1
                    seen_in_source.add(norm)

    for account in _ordered_accounts(seed=seed, linked_accounts=linked_accounts):
        for token in re.findall(r"[0-9a-zа-яё]+", str(account.handle or ""), flags=re.IGNORECASE):
            norm = token.strip().lower().replace("ё", "е")
            stem = _stem_token(norm)
            if len(norm) >= 3 and (stem not in _IDENTITY_SAFE_THEME_STEMS or supporting_counts.get(norm, 0) <= 1):
                blocked.add(norm)

        title_tokens = [
            token.strip().lower().replace("ё", "е")
            for token in re.findall(r"[0-9a-zа-яё]+", str(account.title or ""), flags=re.IGNORECASE)
            if len(token.strip()) >= 3
        ]
        unsupported_title_tokens = [
            token
            for token in title_tokens
            if _stem_token(token) not in _IDENTITY_SAFE_THEME_STEMS or supporting_counts.get(token, 0) <= 1
        ]
        blocked.update(unsupported_title_tokens)

    return blocked


def _supplement_exam_subject_keywords(
    *,
    keywords: list[str],
    keyword_sources: list[KeywordSource],
) -> list[str]:
    if not keywords:
        return []
    if not any(any(marker in keyword.lower() for marker in ("егэ", "огэ", "exam")) for keyword in keywords):
        return keywords

    candidates: dict[str, int] = {}
    for source in keyword_sources:
        for exam, subject in _EXAM_SUBJECT_RE.findall(str(source.text or "")):
            phrase = f"{exam.lower()} по {subject.lower().replace('ё', 'е')}".strip()
            candidates[phrase] = candidates.get(phrase, 0) + 1
    if not candidates:
        return keywords

    best_phrase = sorted(candidates.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))[0][0]
    if any(best_phrase in keyword.lower() for keyword in keywords):
        return keywords
    return [best_phrase] + [keyword for keyword in keywords if keyword.lower() != best_phrase]


def infer_niche_keywords(
    *,
    seed: SeedResolution,
    competitors: list[SeedResolution],
    prefer_llm: bool,
    linked_accounts: list[SeedResolution] | None = None,
    context: SetupRunContext | None = None,
) -> tuple[list[str], str]:
    """
    Returns: (keywords, source) where source in {"llm","auto"}.
    """
    context_text = build_niche_context_text(
        seed=seed,
        competitors=competitors,
        linked_accounts=linked_accounts,
        context=context,
    )
    keyword_sources = build_keyword_sources(
        seed=seed,
        competitors=competitors,
        linked_accounts=linked_accounts,
        context=context,
    )
    auto_keywords = extract_keywords(
        keyword_sources,
        max_keywords=8,
        blocked_terms=build_keyword_blocked_terms(
            seed=seed,
            linked_accounts=linked_accounts,
            keyword_sources=keyword_sources,
        ),
    )
    auto_keywords = _supplement_exam_subject_keywords(keywords=auto_keywords, keyword_sources=keyword_sources)[:8]
    if len(auto_keywords) >= 3 or not prefer_llm:
        return auto_keywords, "auto"

    if prefer_llm:
        try:
            kws = infer_keywords_ru(context_text=context_text)
            if kws:
                return kws, "llm"
        except GeminiError as e:
            logger.warning("LLM niche inference failed: %s", e)
        except Exception as e:
            logger.exception("Unexpected LLM niche inference error: %s", e)

    return auto_keywords, "auto"
