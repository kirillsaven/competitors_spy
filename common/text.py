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
    "new",
    "official",
    "online",
    "real",
    "simple",
    "today",
    "world",
    "уровень",
    "рост",
    "новый",
    "новые",
    "новая",
    "новое",
    "сложный",
    "сложная",
    "сложное",
    "сложных",
    "объяснять",
    "объясню",
    "бояться",
    "лучшее",
    "лучший",
    "лучшие",
    "новости",
    "официальный",
    "простой",
    "реально",
    "сегодня",
    "скоро",
}

_PHRASE_CONNECTORS = {
    "and",
    "for",
    "of",
    "the",
    "to",
    "with",
    "без",
    "для",
    "из",
    "по",
    "про",
    "с",
}

_GENERIC_SINGLETON_STEMS = {
    "bo",
    "explain",
    "fear",
    "level",
    "new",
    "rise",
    "рост",
    "скор",
    "сложн",
    "уров",
    "объясня",
    "боят",
    "нов",
}

_USEFUL_THEME_STEMS = {
    "english",
    "lesson",
    "material",
    "notes",
    "school",
    "student",
    "teacher",
    "tutor",
    "worksheet",
    "английск",
    "граммат",
    "групп",
    "замет",
    "материал",
    "онлайн",
    "преподав",
    "репетитор",
    "урок",
    "ученик",
    "учител",
    "школ",
}

_SOURCE_TYPE_WEIGHTS = {
    "description": 2.8,
    "recent": 1.6,
    "competitor": 0.9,
    "generic": 1.0,
}

_RU_SUFFIXES = (
    "иями",
    "ями",
    "ами",
    "ого",
    "его",
    "ему",
    "ому",
    "ыми",
    "ими",
    "иях",
    "ах",
    "ях",
    "ия",
    "ья",
    "ий",
    "ый",
    "ой",
    "ая",
    "яя",
    "ое",
    "ее",
    "ые",
    "ие",
    "ом",
    "ем",
    "ам",
    "ям",
    "ов",
    "ев",
    "ей",
    "ой",
    "ую",
    "юю",
    "иям",
    "ием",
    "ию",
    "ию",
    "ть",
    "ти",
    "ся",
    "сь",
    "а",
    "я",
    "ы",
    "и",
    "е",
    "о",
    "у",
)

_EN_SUFFIXES = (
    "ments",
    "ation",
    "ities",
    "ingly",
    "ingly",
    "ingly",
    "ings",
    "ment",
    "ions",
    "tion",
    "ness",
    "less",
    "able",
    "ible",
    "edly",
    "edly",
    "edly",
    "ers",
    "ing",
    "ies",
    "ied",
    "est",
    "ers",
    "er",
    "ed",
    "ly",
    "es",
    "s",
)


@dataclass(frozen=True)
class KeywordSource:
    text: str
    source_id: str = ""
    source_type: str = "generic"


@dataclass
class _CandidateStats:
    content_count: int
    source_weight: float = 0.0
    total_count: int = 0
    chunk_ids: set[int] = field(default_factory=set)
    source_ids: set[str] = field(default_factory=set)
    phrase_counts: Counter[str] = field(default_factory=Counter)
    stem_tokens: tuple[str, ...] = ()
    useful_hits: int = 0
    generic_hits: int = 0


def _normalize_token(token: str) -> str:
    return str(token or "").strip().lower().replace("ё", "е")


def _stem_token(token: str) -> str:
    norm = _normalize_token(token)
    if len(norm) <= 4:
        return norm
    if re.search(r"[а-я]", norm):
        for suffix in _RU_SUFFIXES:
            if len(norm) - len(suffix) >= 4 and norm.endswith(suffix):
                return norm[: -len(suffix)]
        return norm
    for suffix in _EN_SUFFIXES:
        if len(norm) - len(suffix) >= 4 and norm.endswith(suffix):
            return norm[: -len(suffix)]
    return norm


def _normalize_blocked_terms(blocked_terms: Iterable[str] | None) -> tuple[set[str], set[str]]:
    tokens: set[str] = set()
    stems: set[str] = set()
    for raw in blocked_terms or []:
        for token in _TOKEN_RE.findall(str(raw or "")):
            norm = _normalize_token(token)
            if len(norm) < 3:
                continue
            tokens.add(norm)
            stems.add(_stem_token(norm))
    return tokens, stems


def _is_connector(token: str) -> bool:
    return _normalize_token(token) in _PHRASE_CONNECTORS


def _is_content_token(token: str, *, blocked_tokens: set[str], blocked_stems: set[str]) -> bool:
    norm = _normalize_token(token)
    if len(norm) < 3 or norm.isdigit():
        return False
    if norm in blocked_tokens or _stem_token(norm) in blocked_stems:
        return False
    if norm in _STOPWORDS_EN or norm in _STOPWORDS_RU or norm in _STRUCTURAL_JUNK or norm in _LOW_INFORMATION:
        return False
    if norm.startswith("http") or norm.startswith("www"):
        return False
    return True


def _segment_tokens(text: str) -> list[list[str]]:
    segments: list[list[str]] = []
    for raw_segment in _SEGMENT_RE.split(text or ""):
        tokens = [_normalize_token(token) for token in _TOKEN_RE.findall(raw_segment)]
        if tokens:
            segments.append(tokens)
    return segments


def _build_candidate_phrase(phrase_tokens: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(phrase_tokens)).strip()


def _token_script(token: str) -> str:
    norm = _normalize_token(token)
    if re.search(r"[а-я]", norm):
        return "ru"
    if re.search(r"[a-z]", norm):
        return "en"
    return "other"


def _iter_candidates(
    tokens: list[str],
    *,
    blocked_tokens: set[str],
    blocked_stems: set[str],
) -> Counter[tuple[str, tuple[str, ...], int, int, int]]:
    candidates: Counter[tuple[str, tuple[str, ...], int, int, int]] = Counter()
    content_positions = [
        idx
        for idx, token in enumerate(tokens)
        if _is_content_token(token, blocked_tokens=blocked_tokens, blocked_stems=blocked_stems)
    ]
    if not content_positions:
        return candidates

    for content_idx in content_positions:
        content_token = tokens[content_idx]
        stem = _stem_token(content_token)
        generic_hits = 1 if stem in _GENERIC_SINGLETON_STEMS else 0
        useful_hits = 1 if stem in _USEFUL_THEME_STEMS else 0
        candidates[(_build_candidate_phrase([content_token]), (stem,), 1, useful_hits, generic_hits)] += 1

    for start in content_positions:
        phrase_tokens: list[str] = []
        content_tokens: list[str] = []
        content_stems: list[str] = []
        useful_hits = 0
        generic_hits = 0
        connector_open = False
        for idx in range(start, len(tokens)):
            token = tokens[idx]
            if _is_content_token(token, blocked_tokens=blocked_tokens, blocked_stems=blocked_stems):
                stem = _stem_token(token)
                phrase_tokens.append(token)
                content_tokens.append(token)
                content_stems.append(stem)
                useful_hits += int(stem in _USEFUL_THEME_STEMS)
                generic_hits += int(stem in _GENERIC_SINGLETON_STEMS)
                connector_open = False
                if 2 <= len(content_tokens) <= 4:
                    if len({_token_script(item) for item in content_tokens}) == 1:
                        candidates[
                            (
                                _build_candidate_phrase(phrase_tokens),
                                tuple(content_stems),
                                len(content_tokens),
                                useful_hits,
                                generic_hits,
                            )
                        ] += 1
                if len(content_tokens) >= 4:
                    break
                continue

            if _is_connector(token) and content_tokens and not connector_open:
                phrase_tokens.append(token)
                connector_open = True
                continue
            break
    return candidates


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
    phrase_bonus = {1: -2.6, 2: 4.3, 3: 5.8, 4: 6.4}.get(stats.content_count, 0.0)
    source_spread = max(len(stats.source_ids) - 1, 0) * 2.6
    chunk_spread = max(len(stats.chunk_ids) - 1, 0) * 1.2
    frequency_bonus = min(stats.total_count, 5) * 0.35
    useful_bonus = min(stats.useful_hits, 2) * 1.3
    generic_penalty = stats.generic_hits * 1.4 if stats.content_count == 1 else 0.0
    return stats.source_weight + phrase_bonus + source_spread + chunk_spread + frequency_bonus + useful_bonus - generic_penalty


def _best_display_phrase(stats: _CandidateStats) -> str:
    ranked = sorted(
        stats.phrase_counts.items(),
        key=lambda item: (
            -item[1],
            -len(item[0].split()),
            -len(item[0]),
            item[0],
        ),
    )
    return ranked[0][0]


def _is_subphrase(candidate_stems: tuple[str, ...], existing_stems: tuple[str, ...]) -> bool:
    if candidate_stems == existing_stems:
        return True
    candidate_set = set(candidate_stems)
    existing_set = set(existing_stems)
    if candidate_set == existing_set:
        return True
    if len(candidate_stems) >= len(existing_stems):
        return False
    return candidate_set.issubset(existing_set)


def extract_keywords(
    text: str | Iterable[KeywordSource | str],
    max_keywords: int = 8,
    *,
    blocked_terms: Iterable[str] | None = None,
) -> list[str]:
    sources = _coerce_sources(text)
    if not sources:
        return []

    blocked_tokens, blocked_stems = _normalize_blocked_terms(blocked_terms)

    candidates: dict[tuple[str, ...], _CandidateStats] = {}
    chunk_index = 0
    for source_index, source in enumerate(sources):
        source_text = str(source.text or "").strip()
        if not source_text:
            continue
        source_id = source.source_id or f"source-{source_index}"
        source_weight = _SOURCE_TYPE_WEIGHTS.get(source.source_type or "generic", 1.0)
        for tokens in _segment_tokens(source_text):
            chunk_index += 1
            for candidate, count in _iter_candidates(
                tokens,
                blocked_tokens=blocked_tokens,
                blocked_stems=blocked_stems,
            ).items():
                phrase, stem_tokens, content_count, useful_hits, generic_hits = candidate
                stats = candidates.setdefault(
                    stem_tokens,
                    _CandidateStats(
                        content_count=content_count,
                        stem_tokens=stem_tokens,
                    ),
                )
                stats.total_count += count
                stats.source_weight += source_weight
                stats.chunk_ids.add(chunk_index)
                stats.source_ids.add(source_id)
                stats.useful_hits = max(stats.useful_hits, useful_hits)
                stats.generic_hits = max(stats.generic_hits, generic_hits)
                stats.phrase_counts[phrase] += count

    ranked = sorted(
        candidates.values(),
        key=lambda stats: (
            -_candidate_score(stats),
            -stats.content_count,
            -len(_best_display_phrase(stats)),
            _best_display_phrase(stats),
        ),
    )

    selected: list[str] = []
    selected_stems: list[tuple[str, ...]] = []
    top_score: float | None = None
    for stats in ranked:
        score = _candidate_score(stats)
        if top_score is None:
            top_score = score
        if stats.content_count == 1:
            single_stem = stats.stem_tokens[0]
            if single_stem in _GENERIC_SINGLETON_STEMS:
                continue
            if len(stats.source_ids) < 2 and len(stats.chunk_ids) < 3:
                continue
        if len(selected) >= 2 and score < max(4.8, (top_score or score) * 0.42):
            break

        if any(_is_subphrase(stats.stem_tokens, existing) for existing in selected_stems):
            continue

        display = _best_display_phrase(stats)
        selected.append(display)
        selected_stems.append(stats.stem_tokens)
        if len(selected) >= max_keywords:
            break
    return selected
