from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable


_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_SEGMENT_RE = re.compile(r"[\n\r.!?;:()\[\]{}|]+")

_STOPWORDS_EN = {
    "a",
    "an",
    "and",
    "are",
    "about",
    "after",
    "all",
    "also",
    "any",
    "at",
    "be",
    "been",
    "being",
    "but",
    "by",
    "can",
    "co",
    "com",
    "for",
    "from",
    "get",
    "had",
    "has",
    "have",
    "how",
    "http",
    "https",
    "into",
    "its",
    "just",
    "more",
    "new",
    "not",
    "now",
    "our",
    "out",
    "over",
    "that",
    "the",
    "their",
    "them",
    "there",
    "this",
    "those",
    "through",
    "today",
    "video",
    "videos",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "www",
    "you",
    "your",
}

_STOPWORDS_RU = {
    "а",
    "без",
    "более",
    "будет",
    "бы",
    "был",
    "была",
    "были",
    "быть",
    "в",
    "вам",
    "вас",
    "весь",
    "во",
    "вот",
    "все",
    "всё",
    "вы",
    "где",
    "да",
    "даже",
    "для",
    "до",
    "его",
    "ее",
    "её",
    "если",
    "есть",
    "еще",
    "ещё",
    "за",
    "здесь",
    "и",
    "из",
    "или",
    "им",
    "их",
    "к",
    "как",
    "ко",
    "когда",
    "кто",
    "ли",
    "либо",
    "мне",
    "можно",
    "мы",
    "на",
    "над",
    "надо",
    "набор",
    "наш",
    "не",
    "него",
    "нее",
    "неё",
    "них",
    "но",
    "ну",
    "о",
    "об",
    "однако",
    "он",
    "она",
    "они",
    "оно",
    "от",
    "очень",
    "по",
    "под",
    "после",
    "потом",
    "потому",
    "почти",
    "при",
    "просто",
    "про",
    "раз",
    "с",
    "сам",
    "самое",
    "самый",
    "свой",
    "себя",
    "сейчас",
    "так",
    "также",
    "там",
    "тебя",
    "тем",
    "то",
    "того",
    "тоже",
    "только",
    "том",
    "тут",
    "ты",
    "у",
    "уже",
    "хотя",
    "чего",
    "чей",
    "чем",
    "через",
    "что",
    "чтобы",
    "эта",
    "эти",
    "это",
    "этот",
    "я",
}

_STRUCTURAL_JUNK = {
    "account",
    "accounts",
    "bio",
    "channel",
    "channels",
    "content",
    "creator",
    "creators",
    "description",
    "display",
    "feed",
    "handle",
    "handles",
    "http",
    "https",
    "instagram",
    "link",
    "links",
    "nickname",
    "platform",
    "post",
    "posts",
    "profile",
    "profiles",
    "reel",
    "reels",
    "seed",
    "short",
    "shorts",
    "telegram",
    "tiktok",
    "title",
    "url",
    "urls",
    "username",
    "video",
    "videos",
    "youtube",
    "ютуб",
    "тикток",
    "инстаграм",
    "аккаунт",
    "аккаунты",
    "био",
    "канал",
    "каналы",
    "платформа",
    "профиль",
    "профили",
    "ссылка",
    "ссылки",
    "сид",
    "титул",
    "урл",
    "хендл",
}

_LOW_INFORMATION = {
    "best",
    "daily",
    "official",
    "online",
    "real",
    "simple",
    "today",
    "world",
    "лучшее",
    "лучший",
    "новое",
    "новости",
    "официальный",
    "простой",
    "реально",
    "сегодня",
}

_SOURCE_TYPE_WEIGHTS = {
    "description": 2.4,
    "recent": 1.4,
    "competitor": 0.8,
    "generic": 1.0,
}


@dataclass(frozen=True)
class KeywordSource:
    text: str
    source_id: str = ""
    source_type: str = "generic"


@dataclass
class _CandidateStats:
    token_count: int
    source_weight: float = 0.0
    total_count: int = 0
    chunk_ids: set[int] = field(default_factory=set)
    source_ids: set[str] = field(default_factory=set)


def _normalize_token(token: str) -> str:
    return str(token or "").strip().lower().replace("ё", "е")


def _is_keyword_token(token: str) -> bool:
    norm = _normalize_token(token)
    if len(norm) < 3 or norm.isdigit():
        return False
    if norm in _STOPWORDS_EN or norm in _STOPWORDS_RU or norm in _STRUCTURAL_JUNK or norm in _LOW_INFORMATION:
        return False
    if norm.startswith("http") or norm.startswith("www"):
        return False
    return True


def _segment_tokens(text: str) -> list[list[str]]:
    segments: list[list[str]] = []
    for raw_segment in _SEGMENT_RE.split(text or ""):
        run: list[str] = []
        for token in _TOKEN_RE.findall(raw_segment):
            norm = _normalize_token(token)
            if _is_keyword_token(norm):
                run.append(norm)
                continue
            if run:
                segments.append(run)
                run = []
        if run:
            segments.append(run)
    return segments


def _iter_phrases(tokens: list[str]) -> Counter[str]:
    phrases: Counter[str] = Counter()
    for size in (3, 2, 1):
        if len(tokens) < size:
            continue
        for start in range(0, len(tokens) - size + 1):
            phrase_tokens = tokens[start : start + size]
            if len(set(phrase_tokens)) != len(phrase_tokens):
                continue
            phrase = " ".join(phrase_tokens)
            phrases[phrase] += 1
    return phrases


def _coerce_sources(text: str | Iterable[KeywordSource | str]) -> list[KeywordSource]:
    if isinstance(text, str):
        return [KeywordSource(text=text)]

    out: list[KeywordSource] = []
    for idx, item in enumerate(text):
        if isinstance(item, KeywordSource):
            out.append(item)
            continue
        out.append(KeywordSource(text=str(item or ""), source_id=f"source-{idx}", source_type="generic"))
    return out


def _candidate_score(stats: _CandidateStats) -> float:
    phrase_bonus = {1: 0.0, 2: 1.8, 3: 2.7}.get(stats.token_count, 0.0)
    source_spread = max(len(stats.source_ids) - 1, 0) * 2.3
    chunk_spread = max(len(stats.chunk_ids) - 1, 0) * 1.1
    frequency_bonus = min(stats.total_count, 4) * 0.35
    return stats.source_weight + source_spread + chunk_spread + frequency_bonus + phrase_bonus


def _should_skip_phrase(phrase: str, *, selected: list[str]) -> bool:
    tokens = set(phrase.split())
    for existing in selected:
        existing_tokens = set(existing.split())
        if phrase == existing:
            return True
        if len(tokens) == 1 and tokens.issubset(existing_tokens):
            return True
        if len(tokens) > 1 and tokens.issubset(existing_tokens):
            return True
    return False


def extract_keywords(text: str | Iterable[KeywordSource | str], max_keywords: int = 8) -> list[str]:
    sources = _coerce_sources(text)
    if not sources:
        return []

    candidates: dict[str, _CandidateStats] = {}
    chunk_index = 0
    for source_index, source in enumerate(sources):
        source_text = str(source.text or "").strip()
        if not source_text:
            continue
        source_id = source.source_id or f"source-{source_index}"
        source_weight = _SOURCE_TYPE_WEIGHTS.get(source.source_type or "generic", 1.0)
        for tokens in _segment_tokens(source_text):
            chunk_index += 1
            for phrase, count in _iter_phrases(tokens).items():
                stats = candidates.setdefault(phrase, _CandidateStats(token_count=len(phrase.split())))
                stats.total_count += count
                stats.source_weight += source_weight
                stats.chunk_ids.add(chunk_index)
                stats.source_ids.add(source_id)

    ranked = sorted(
        candidates.items(),
        key=lambda item: (
            -_candidate_score(item[1]),
            -item[1].token_count,
            -len(item[0]),
            item[0],
        ),
    )

    selected: list[str] = []
    for phrase, stats in ranked:
        if stats.token_count == 1 and len(stats.source_ids) == 1 and stats.total_count == 1:
            continue
        if _should_skip_phrase(phrase, selected=selected):
            continue
        selected.append(phrase)
        if len(selected) >= max_keywords:
            break
    return selected
