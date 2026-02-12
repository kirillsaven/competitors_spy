from __future__ import annotations

import re
from collections import Counter


_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)

_STOPWORDS_EN = {
    "the",
    "and",
    "for",
    "with",
    "you",
    "your",
    "from",
    "this",
    "that",
    "are",
    "was",
    "were",
    "what",
    "how",
    "why",
    "when",
    "where",
    "into",
    "about",
    "video",
    "videos",
    "channel",
    "new",
}

_STOPWORDS_RU = {
    "и",
    "в",
    "во",
    "не",
    "что",
    "он",
    "она",
    "оно",
    "они",
    "мы",
    "вы",
    "я",
    "ты",
    "на",
    "по",
    "за",
    "к",
    "у",
    "о",
    "об",
    "от",
    "до",
    "из",
    "для",
    "как",
    "это",
    "то",
    "а",
    "но",
    "или",
    "про",
    "видео",
    "канал",
    "новый",
}


def extract_keywords(text: str, max_keywords: int = 8) -> list[str]:
    tokens = [t.lower() for t in _TOKEN_RE.findall(text or "")]
    tokens = [t for t in tokens if len(t) >= 3 and t not in _STOPWORDS_EN and t not in _STOPWORDS_RU]
    if not tokens:
        return []
    counts = Counter(tokens)
    return [w for w, _ in counts.most_common(max_keywords)]

