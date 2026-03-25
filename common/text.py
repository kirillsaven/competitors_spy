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
    "анкета",
    "заполняй",
    "запишись",
    "заявка",
    "меньше",
    "подробности",
    "позже",
    "помогаю",
    "присоединиться",
    "своего",
    "своим",
    "ссылка",
    "ссылке",
    "смотри",
    "оставляй",
    "найди",
    "найти",
    "говорят",
    "иногда",
    "пройти",
    "тг",
    "тгк",
    "уроке",
    "уроки",
    "английскийонлайн",
    "коллег",
    "открываю",
    "помогла",
    "растет",
    "свою",
    "учеба",
    "чудесных",
    "моей",
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

_SEARCH_UTILITY_THEME_GROUPS = {
    "audience": {
        "beginner",
        "coach",
        "creator",
        "founder",
        "marketer",
        "parent",
        "student",
        "teacher",
        "tutor",
        "ученик",
        "учител",
        "преподав",
        "репетитор",
        "эксперт",
    },
    "subject": {
        "business",
        "english",
        "grammar",
        "language",
        "lesson",
        "marketing",
        "pronunciation",
        "science",
        "speaking",
        "английск",
        "граммат",
        "разговор",
        "язык",
    },
    "format": {
        "breakdown",
        "clips",
        "explainer",
        "group",
        "guide",
        "notes",
        "review",
        "worksheet",
        "групп",
        "замет",
        "материал",
        "план",
        "разбор",
        "урок",
    },
    "offer": {
        "course",
        "community",
        "practice",
        "program",
        "school",
        "training",
        "коммюнит",
        "курс",
        "обуч",
        "онлайн",
        "практик",
        "школ",
    },
    "problem": {
        "goal",
        "improve",
        "motivation",
        "problem",
        "results",
        "подготов",
        "повыс",
        "результ",
        "цель",
    },
}

_ACTION_QUERY_STEMS = {
    "begin",
    "book",
    "check",
    "click",
    "explain",
    "find",
    "help",
    "join",
    "leave",
    "look",
    "sign",
    "watch",
    "boят",
    "замеча",
    "заполня",
    "записа",
    "нача",
    "объясня",
    "оставля",
    "помога",
    "посмотр",
    "присоедин",
    "смотр",
}

_SOURCE_TYPE_WEIGHTS = {
    "description": 2.8,
    "recent": 1.6,
    "title": 2.1,
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


@dataclass(frozen=True)
class _IdentityProfile:
    phrases: set[str] = field(default_factory=set)
    tokens: set[str] = field(default_factory=set)
    stems: set[str] = field(default_factory=set)


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
    action_hits: int = 0
    source_types: set[str] = field(default_factory=set)
    utility_groups: set[str] = field(default_factory=set)


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


def _build_identity_profile(identity_terms: Iterable[str] | None) -> _IdentityProfile:
    phrases: set[str] = set()
    tokens: set[str] = set()
    stems: set[str] = set()
    for raw in identity_terms or []:
        raw_text = str(raw or "").strip()
        if not raw_text:
            continue
        phrase_tokens = [_normalize_token(token) for token in _TOKEN_RE.findall(raw_text) if len(_normalize_token(token)) >= 3]
        if phrase_tokens:
            phrases.add(" ".join(phrase_tokens))
        for token in phrase_tokens:
            tokens.add(token)
            stems.add(_stem_token(token))
    return _IdentityProfile(phrases=phrases, tokens=tokens, stems=stems)


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


def _phrase_key(phrase: str) -> str:
    return _build_candidate_phrase([_normalize_token(token) for token in _TOKEN_RE.findall(phrase)])


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
) -> Counter[tuple[str, tuple[str, ...], int, int, int, int, tuple[str, ...]]]:
    candidates: Counter[tuple[str, tuple[str, ...], int, int, int, int, tuple[str, ...]]] = Counter()
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
        action_hits = 1 if stem in _ACTION_QUERY_STEMS else 0
        utility_groups = tuple(
            group
            for group, stems in _SEARCH_UTILITY_THEME_GROUPS.items()
            if stem in stems
        )
        candidates[(_build_candidate_phrase([content_token]), (stem,), 1, useful_hits, generic_hits, action_hits, utility_groups)] += 1

    for start in content_positions:
        phrase_tokens: list[str] = []
        content_tokens: list[str] = []
        content_stems: list[str] = []
        useful_hits = 0
        generic_hits = 0
        action_hits = 0
        utility_groups: set[str] = set()
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
                action_hits += int(stem in _ACTION_QUERY_STEMS)
                for group, stems in _SEARCH_UTILITY_THEME_GROUPS.items():
                    if stem in stems:
                        utility_groups.add(group)
                connector_open = False
                if 2 <= len(content_tokens) <= 5:
                    if len({_token_script(item) for item in content_tokens}) == 1:
                        candidates[
                            (
                                _build_candidate_phrase(phrase_tokens),
                                tuple(content_stems),
                                len(content_tokens),
                                useful_hits,
                                generic_hits,
                                action_hits,
                                tuple(sorted(utility_groups)),
                            )
                        ] += 1
                if len(content_tokens) >= 5:
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
    raise NotImplementedError("_candidate_score requires identity profile")


def _identity_penalty(stats: _CandidateStats, identity: _IdentityProfile) -> float:
    if not identity.stems:
        return 0.0
    overlap = set(stats.stem_tokens) & identity.stems
    if not overlap:
        return 0.0
    overlap_ratio = len(overlap) / max(len(set(stats.stem_tokens)), 1)
    penalty = overlap_ratio * (2.0 + len(overlap) * 0.7)
    if _phrase_key(_best_display_phrase(stats)) in identity.phrases:
        penalty += 5.4 if len(stats.utility_groups) == 0 else 2.4
    if overlap_ratio >= 0.65 and len(stats.source_ids) <= 1:
        penalty += 2.2
    if overlap_ratio == 1.0 and len(stats.utility_groups) < 2 and len(stats.chunk_ids) <= 2:
        penalty += 3.0
    support_relief = max(len(stats.source_ids) - 1, 0) * 1.4 + max(len(stats.chunk_ids) - 1, 0) * 0.55
    return max(penalty - support_relief, 0.0)


def _candidate_score_with_identity(
    stats: _CandidateStats,
    identity: _IdentityProfile,
    stem_contexts: dict[str, set[tuple[str, ...]]],
) -> float:
    phrase_bonus = {1: -9.5, 2: 5.2, 3: 7.0, 4: 7.4, 5: 6.8}.get(stats.content_count, 0.0)
    source_spread = max(len(stats.source_ids) - 1, 0) * 2.8
    chunk_spread = max(len(stats.chunk_ids) - 1, 0) * 1.0
    frequency_bonus = min(stats.total_count, 6) * (0.2 if stats.content_count == 1 else 0.45)
    weighted_source_bonus = min(stats.source_weight, 10.0) * 0.25
    source_type_bonus = 2.4 if "recent" in stats.source_types and ({"description", "title"} & stats.source_types) else 0.0
    utility_group_bonus = len(stats.utility_groups) * 1.45
    combo_bonus = 2.6 if len(stats.utility_groups) >= 2 else 0.0
    useful_bonus = min(stats.useful_hits, 3) * 0.9
    generic_penalty = stats.generic_hits * (1.8 if stats.content_count == 1 else 0.7)
    action_penalty = stats.action_hits * (1.5 if len(stats.utility_groups) < 2 else 0.35)
    identity_penalty = _identity_penalty(stats, identity)
    weak_query_penalty = 2.2 if len(stats.utility_groups) < 2 and len(stats.source_ids) <= 1 and len(stats.chunk_ids) <= 2 else 0.0
    context_sizes = [
        len(stem_contexts.get(stem, {stats.stem_tokens}))
        for stem in set(stats.stem_tokens)
    ]
    context_diversity = sum(context_sizes) / len(context_sizes) if context_sizes else 1.0
    context_bonus = 1.4 if context_diversity >= 2.0 else 0.0
    narrow_identity_penalty = 4.5 if context_diversity <= 1.05 and len(stats.utility_groups) == 0 and len(stats.source_ids) <= 2 else 0.0
    return (
        weighted_source_bonus
        + phrase_bonus
        + source_spread
        + chunk_spread
        + frequency_bonus
        + source_type_bonus
        + utility_group_bonus
        + combo_bonus
        + useful_bonus
        + context_bonus
        - generic_penalty
        - action_penalty
        - identity_penalty
        - weak_query_penalty
        - narrow_identity_penalty
    )


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
    identity_terms: Iterable[str] | None = None,
) -> list[str]:
    sources = _coerce_sources(text)
    if not sources:
        return []

    blocked_tokens, blocked_stems = _normalize_blocked_terms(blocked_terms)
    identity = _build_identity_profile(identity_terms)

    candidates: dict[tuple[str, ...], _CandidateStats] = {}
    stem_contexts: dict[str, set[tuple[str, ...]]] = {}
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
                phrase, stem_tokens, content_count, useful_hits, generic_hits, action_hits, utility_groups = candidate
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
                stats.action_hits = max(stats.action_hits, action_hits)
                stats.source_types.add(source.source_type or "generic")
                stats.utility_groups.update(utility_groups)
                stats.phrase_counts[phrase] += count
                for stem in set(stem_tokens):
                    stem_contexts.setdefault(stem, set()).add(stem_tokens)

    ranked = sorted(
        candidates.values(),
        key=lambda stats: (
            -_candidate_score_with_identity(stats, identity, stem_contexts),
            -stats.content_count,
            -len(_best_display_phrase(stats)),
            _best_display_phrase(stats),
        ),
    )

    selected: list[str] = []
    selected_stems: list[tuple[str, ...]] = []
    selected_scores: list[float] = []
    top_score: float | None = None
    for stats in ranked:
        score = _candidate_score_with_identity(stats, identity, stem_contexts)
        if top_score is None:
            top_score = score
        display = _best_display_phrase(stats)
        if _phrase_key(display) in identity.phrases and len(stats.utility_groups) == 0:
            continue
        if stats.content_count == 1:
            single_stem = stats.stem_tokens[0]
            if single_stem in _GENERIC_SINGLETON_STEMS:
                continue
            if len(stats.source_ids) < 2 and len(stats.chunk_ids) < 3:
                continue
            if len(stats.utility_groups) < 2 or score < 13.0 or len(stats.chunk_ids) < 4:
                continue
        if len(selected) >= 4 and score < max(5.2, (top_score or score) * 0.34):
            break

        replaced_existing = False
        for idx, (existing, existing_score) in enumerate(zip(selected_stems, selected_scores, strict=False)):
            existing_set = set(existing)
            candidate_set = set(stats.stem_tokens)
            if existing_set.issubset(candidate_set) and len(candidate_set) > len(existing_set) and score >= existing_score - 2.5:
                selected[idx] = _best_display_phrase(stats)
                selected_stems[idx] = stats.stem_tokens
                selected_scores[idx] = score
                replaced_existing = True
                break
        if replaced_existing:
            continue

        if any(
            _is_subphrase(stats.stem_tokens, existing) and score <= existing_score + 1.0
            for existing, existing_score in zip(selected_stems, selected_scores, strict=False)
        ):
            continue

        selected.append(display)
        selected_stems.append(stats.stem_tokens)
        selected_scores.append(score)
        if len(selected) >= max_keywords:
            break
    return selected
