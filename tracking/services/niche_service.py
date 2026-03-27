from __future__ import annotations

import logging
import re

from common.text import KeywordSource, extract_keywords

from tracking.adapters.base import SeedResolution
from tracking.services.platform_onboarding import get_recent_seed_content_texts as platform_get_recent_seed_content_texts
from tracking.services.youtube_service import get_recent_video_titles, infer_youtube_keywords

logger = logging.getLogger(__name__)

_NOISE_KEYWORDS = {
    "seed",
    "platform",
    "handle",
    "channel",
    "video",
    "videos",
    "youtube",
    "official",
}

_TOPIC_HINTS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (
        re.compile(
            r"(английск|уроки английского|изучение английского|репетитор|разговорный английский)",
            re.IGNORECASE,
        ),
        ("английский язык", "уроки английского"),
    ),
    (
        re.compile(
            r"(преподавател|репетитор|teacher|tutor|планы уроков|lesson plans)",
            re.IGNORECASE,
        ),
        ("преподаватели английского", "репетиторы английского", "планы уроков"),
    ),
    (
        re.compile(
            r"\b(english|speaking|grammar|vocabulary|pronunciation|ielts|toefl|language learning|learn english)\b",
            re.IGNORECASE,
        ),
        ("english language", "language learning", "english lessons", "spoken english"),
    ),
    (
        re.compile(
            r"\b(engineer|engineering|mechanical|robot|robotics|nasa|science|experiment|physics|chemistry|rover|machine|diy|maker)\b",
            re.IGNORECASE,
        ),
        ("engineering projects", "science experiments", "robotics", "DIY projects", "mechanics"),
    ),
)


def _clean_niche_keywords(keywords: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        value = str(kw or "").strip()
        if not value:
            continue
        key = value.lower()
        if len(key) < 3:
            continue
        if key in _NOISE_KEYWORDS:
            continue
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
    return cleaned[:12]


def _trim_keywords(keywords: list[str], limit: int = 6) -> list[str]:
    return _clean_niche_keywords(keywords)[:limit]


def get_recent_seed_content_texts(*, seed: SeedResolution, n: int = 10) -> list[str]:
    if seed.platform == "youtube":
        return get_recent_video_titles(seed, n=n)
    return platform_get_recent_seed_content_texts(seed=seed, n=n)


def _prioritize_keywords(*, primary: list[str], topic_phrases: list[str], secondary: list[str] | None = None) -> list[str]:
    secondary = list(secondary or [])
    combined = list(primary[:2]) + list(topic_phrases) + list(primary[2:]) + secondary
    return _trim_keywords(combined)


def _derive_topic_phrases(text: str) -> list[str]:
    phrases: list[str] = []
    seen: set[str] = set()
    raw = str(text or "").strip()
    if not raw:
        return []

    for pattern, candidates in _TOPIC_HINTS:
        if not pattern.search(raw):
            continue
        for phrase in candidates:
            key = phrase.lower()
            if key in seen:
                continue
            seen.add(key)
            phrases.append(phrase)
    return phrases[:8]


def is_niche_keywords_poor(*, keywords: list[str], seed: SeedResolution | None = None) -> bool:
    kws = _trim_keywords(list(keywords or []), limit=12)
    if len(kws) < 4:
        return True

    phrase_count = sum(1 for k in kws if " " in k)
    cyr_count = sum(1 for k in kws if re.search(r"[а-яё]", k.lower()))
    if phrase_count >= 2 and len(kws) >= 5:
        return False

    if phrase_count == 0 and cyr_count <= 1:
        return True

    if seed and seed.title:
        seed_tokens = set(extract_keywords(seed.title, max_keywords=8))
        kw_tokens = set(extract_keywords("\n".join(kws), max_keywords=20))
        if seed_tokens and kw_tokens and len(seed_tokens & kw_tokens) == 0 and cyr_count == 0:
            return True

    return False


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


def build_niche_fallback_text(
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
        if account.handle:
            _append_unique(parts, account.handle, seen_parts)
        for text in get_recent_seed_content_texts(seed=account, n=10):
            _append_unique(parts, text, seen_parts)

    for competitor in competitors[:20]:
        if competitor.title:
            _append_unique(parts, competitor.title, seen_parts)
        if competitor.description:
            _append_unique(parts, competitor.description, seen_parts)
        if competitor.handle:
            _append_unique(parts, competitor.handle, seen_parts)

    return "\n".join(part for part in parts if part).strip()


def build_keyword_source_text(
    *,
    seed: SeedResolution,
    competitors: list[SeedResolution],
    linked_accounts: list[SeedResolution] | None = None,
) -> str:
    return build_niche_fallback_text(seed=seed, competitors=competitors, linked_accounts=linked_accounts)


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
        if account.description:
            _append_keyword_source(
                sources,
                text=account.description,
                source_id=source_id,
                source_type="description",
                seen=seen_sources,
            )
        for text in get_recent_seed_content_texts(seed=account, n=10):
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
    for source in keyword_sources or []:
        for token in re.findall(r"[0-9a-zа-яё]+", str(source.text or ""), flags=re.IGNORECASE):
            norm = token.strip().lower().replace("ё", "е")
            if len(norm) >= 3:
                supporting_tokens.add(norm)

    for account in _ordered_accounts(seed=seed, linked_accounts=linked_accounts):
        for token in re.findall(r"[0-9a-zа-яё]+", str(account.handle or ""), flags=re.IGNORECASE):
            norm = token.strip().lower().replace("ё", "е")
            if len(norm) >= 3 and norm not in supporting_tokens:
                blocked.add(norm)

        title_tokens = [
            token.strip().lower().replace("ё", "е")
            for token in re.findall(r"[0-9a-zа-яё]+", str(account.title or ""), flags=re.IGNORECASE)
            if len(token.strip()) >= 3
        ]
        blocked.update(token for token in title_tokens if token not in supporting_tokens)

    return blocked


def infer_niche_keywords(
    *,
    seed: SeedResolution,
    competitors: list[SeedResolution],
    linked_accounts: list[SeedResolution] | None = None,
) -> tuple[list[str], str]:
    """Returns: (keywords, source) where source is always {"auto"}."""
    context = build_niche_context_text(seed=seed, competitors=competitors, linked_accounts=linked_accounts)
    keyword_sources = build_keyword_sources(seed=seed, competitors=competitors, linked_accounts=linked_accounts)
    auto_keywords = _clean_niche_keywords(
        extract_keywords(
            keyword_sources,
            max_keywords=6,
            blocked_terms=build_keyword_blocked_terms(
                seed=seed,
                linked_accounts=linked_accounts,
                keyword_sources=keyword_sources,
            ),
        )
    )

    fallback_text = build_niche_fallback_text(seed=seed, competitors=competitors, linked_accounts=linked_accounts)
    topic_phrases = _derive_topic_phrases(fallback_text)

    if seed.platform == "youtube":
        try:
            youtube_keywords = infer_youtube_keywords(seed)
            if youtube_keywords:
                kws = _prioritize_keywords(primary=auto_keywords, topic_phrases=topic_phrases, secondary=youtube_keywords)
                if kws and not is_niche_keywords_poor(keywords=kws, seed=seed):
                    return kws, "auto"
        except Exception as e:
            logger.warning("YouTube heuristic niche inference failed: %s", e)

    if auto_keywords:
        kws = _prioritize_keywords(primary=auto_keywords, topic_phrases=topic_phrases)
        if kws and not is_niche_keywords_poor(keywords=kws, seed=seed):
            return kws, "auto"

    kws = _trim_keywords(topic_phrases + (extract_keywords(fallback_text, max_keywords=8) if fallback_text else []))
    if not kws:
        kws = _trim_keywords(extract_keywords(context, max_keywords=8))
    return kws, "auto"
