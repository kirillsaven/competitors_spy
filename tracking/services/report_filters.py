from __future__ import annotations

import re

from tracking.models import ContentItem, TgUser

_MULTISPACE_RE = re.compile(r"\s+")


def normalize_stopword(value: str | None) -> str:
    return _MULTISPACE_RE.sub(" ", str(value or "").strip().lower())


def normalize_stopwords(values: list[str] | tuple[str, ...] | None) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values or []:
        item = normalize_stopword(value)
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def parse_stopwords_input(raw_text: str | None) -> list[str]:
    parts = [part for part in re.split(r"[\n,]+", str(raw_text or "")) if str(part).strip()]
    return normalize_stopwords(parts)


def get_user_report_stopwords(*, user: TgUser) -> list[str]:
    return normalize_stopwords(user.report_stopwords if isinstance(user.report_stopwords, list) else [])


def normalize_match_text(*, title: str | None, description: str | None) -> str:
    return normalize_stopword(f"{str(title or '').strip()} {str(description or '').strip()}")


def content_matches_stopwords(*, title: str | None, description: str | None, stopwords: list[str]) -> bool:
    normalized_stopwords = normalize_stopwords(stopwords)
    if not normalized_stopwords:
        return False
    haystack = normalize_match_text(title=title, description=description)
    if not haystack:
        return False
    return any(stopword in haystack for stopword in normalized_stopwords)


def filter_scored_items_for_stopwords(*, scored: list[object], stopwords: list[str]) -> list[object]:
    normalized_stopwords = normalize_stopwords(stopwords)
    if not normalized_stopwords:
        return list(scored)
    filtered: list[object] = []
    for item in scored:
        content_item = getattr(item, "content_item", None)
        title = getattr(content_item, "title", None)
        description = getattr(content_item, "description", None)
        if content_matches_stopwords(title=title, description=description, stopwords=normalized_stopwords):
            continue
        filtered.append(item)
    return filtered


def filter_content_items_for_stopwords(*, items: list[ContentItem], stopwords: list[str]) -> list[ContentItem]:
    normalized_stopwords = normalize_stopwords(stopwords)
    if not normalized_stopwords:
        return list(items)
    return [
        item
        for item in items
        if not content_matches_stopwords(
            title=getattr(item, "title", None),
            description=getattr(item, "description", None),
            stopwords=normalized_stopwords,
        )
    ]
