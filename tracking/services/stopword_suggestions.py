from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta

from django.utils import timezone

from common.text import _STOPWORDS_EN, _STOPWORDS_RU
from tracking.models import TgUser
from tracking.services.report_filters import get_user_report_stopwords, normalize_stopword
from tracking.services.report_pipeline import build_report_preview, build_setup_verification_preview


_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_BANNED_TOKENS = set(_STOPWORDS_EN) | set(_STOPWORDS_RU) | {
    "short",
    "shorts",
    "video",
    "videos",
}


def _extract_candidate_phrases(*, texts: list[str], limit: int) -> list[str]:
    counts: Counter[str] = Counter()
    order: list[str] = []
    seen: set[str] = set()

    for raw_text in texts:
        tokens = [
            token.lower()
            for token in _TOKEN_RE.findall(str(raw_text or ""))
            if len(token) >= 3 and token.lower() not in _BANNED_TOKENS
        ]
        local_seen: set[str] = set()
        for idx in range(len(tokens) - 1):
            phrase = f"{tokens[idx]} {tokens[idx + 1]}"
            if phrase in local_seen:
                continue
            local_seen.add(phrase)
            counts[phrase] += 1
            if phrase not in seen:
                seen.add(phrase)
                order.append(phrase)

    ranked = sorted(order, key=lambda phrase: (-counts[phrase], order.index(phrase)))
    return ranked[:limit]


def build_user_stopword_suggestions(*, user: TgUser, limit: int = 12) -> list[str]:
    current = set(get_user_report_stopwords(user=user))
    period_end = timezone.now()
    period_start = period_end - timedelta(hours=24)

    report_preview = build_report_preview(user=user, period_start=period_start, period_end=period_end)
    report_titles = [
        str(item.get("title") or "").strip()
        for section in (report_preview.payload.get("sections") or [])
        for item in (section.get("items") or [])
        if str(item.get("title") or "").strip()
    ]
    suggestions = _extract_candidate_phrases(texts=report_titles, limit=limit * 2)

    if len(suggestions) < limit:
        setup_preview = build_setup_verification_preview(user=user, period_end=period_end)
        setup_titles = [
            str((entry.get("latest_item") or {}).get("title") or "").strip()
            for section in (setup_preview.payload.get("sections") or [])
            for entry in (section.get("entries") or [])
            if str((entry.get("latest_item") or {}).get("title") or "").strip()
        ]
        suggestions.extend(_extract_candidate_phrases(texts=setup_titles, limit=limit * 2))

    out: list[str] = []
    seen: set[str] = set()
    for phrase in suggestions:
        normalized = normalize_stopword(phrase)
        if not normalized or normalized in current or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
        if len(out) >= limit:
            break
    return out
