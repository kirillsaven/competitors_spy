from __future__ import annotations

import logging
import re

from common.text import (
    KeywordSource,
    _LOW_INFORMATION,
    _STOPWORDS_EN,
    _STOPWORDS_RU,
    _STRUCTURAL_JUNK,
    _USEFUL_THEME_STEMS,
    _TOKEN_RE,
    _is_content_token,
    _normalize_blocked_terms,
    _normalize_token,
    _stem_token,
    extract_keywords,
)

from tracking.adapters.base import SeedResolution
from tracking.services.platform_onboarding import get_recent_seed_content_texts
from tracking.services.setup_runtime import SetupRunContext
from tracking.services.youtube_service import get_recent_video_titles, infer_youtube_keywords

logger = logging.getLogger(__name__)
_NOISE_KEYWORDS = {
    "seed",
    "platform",
    "handle",
    "channel",
    "main",
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
            r"(разговорн|заговор|speaking practice|spoken english)",
            re.IGNORECASE,
        ),
        ("разговорный английский",),
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
_IDENTITY_SAFE_THEME_STEMS = set(_USEFUL_THEME_STEMS) | {"ege", "exam", "егэ", "огэ", "экзам"}
_EXAM_SUBJECT_RE = re.compile(r"\b(егэ|огэ)\s+по\s+([0-9a-zа-яё-]{3,})", flags=re.IGNORECASE)
_FOR_BEGINNERS_RE = re.compile(r"\b(для\s+начинающ[0-9a-zа-яё-]*|с\s+нуля|начальн[0-9a-zа-яё-]*\s+уров[0-9a-zа-яё-]*)", flags=re.IGNORECASE)
_FOR_ADULTS_RE = re.compile(r"\b(для\s+взросл[0-9a-zа-яё-]*|преподавать\s+взросл[0-9a-zа-яё-]*)", flags=re.IGNORECASE)
_CONVERSATIONAL_RE = re.compile(r"\b(разговорн[0-9a-zа-яё-]*|заговор[0-9a-zа-яё-]*)", flags=re.IGNORECASE)
_LESSON_RE = re.compile(r"\b(урок[0-9a-zа-яё-]*|lesson[s]?|изучен[0-9a-zа-яё-]*)", flags=re.IGNORECASE)
_SCHOOL_RE = re.compile(r"\b(школ[0-9a-zа-яё-]*|school|academy)", flags=re.IGNORECASE)
_TEACHER_RE = re.compile(r"\b(репетитор[0-9a-zа-яё-]*|преподавател[0-9a-zа-яё-]*|teacher|tutor)", flags=re.IGNORECASE)
_GUIDE_RE = re.compile(r"\b(гайд[0-9a-zа-яё-]*|guide[s]?|разбор[0-9a-zа-яё-]*|патч[0-9a-zа-яё-]*|meta|мет[ао][0-9a-zа-яё-]*)", flags=re.IGNORECASE)
_ACCOUNT_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", flags=re.IGNORECASE)
_UTILITY_JUNK_STEMS = {
    "comment",
    "consider",
    "creator",
    "follow",
    "free",
    "join",
    "link",
    "найд",
    "ссылк",
    "профил",
    "оставля",
    "заяв",
    "человек",
    "contact",
    "fork",
    "gmail",
    "inside",
    "провод",
    "risk",
    "врем",
    "лучш",
    "представител",
    "мир",
    "пут",
    "путь",
    "send",
    "signup",
    "subscrib",
    "workshop",
    "топ",
}
_SOURCE_PHRASE_SPLIT_RE = re.compile(r"[\n\r.!?;:,()\[\]{}|]+")
_BOILERPLATE_SOURCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bmy main account is\b", re.IGNORECASE),
    re.compile(r"\bsend me stuff\b", re.IGNORECASE),
    re.compile(r"\banything sent to the above address\b", re.IGNORECASE),
    re.compile(r"\bcontact\s*:\b", re.IGNORECASE),
    re.compile(r"\bgmail dot com\b", re.IGNORECASE),
    re.compile(r"\bat your own risk\b", re.IGNORECASE),
    re.compile(r"\b(?:global|brand) ambassador\b", re.IGNORECASE),
    re.compile(r"\b\d{2,6}\s+[A-Za-z][A-Za-z0-9. ]+\b(?:st|street|suite|ave|avenue|road|rd)\b", re.IGNORECASE),
    re.compile(r"\baffiliate advertising program\b", re.IGNORECASE),
    re.compile(r"\bamazon services llc associates program\b", re.IGNORECASE),
    re.compile(r"\bas an amazon associate\b", re.IGNORECASE),
    re.compile(r"\bearn advertising fees\b", re.IGNORECASE),
    re.compile(r"\bqualifying purchases\b", re.IGNORECASE),
    re.compile(r"\bmay contain affiliate links?\b", re.IGNORECASE),
    re.compile(r"\bi may earn (a )?commission\b", re.IGNORECASE),
    re.compile(r"\bfor entertainment purposes only\b", re.IGNORECASE),
)


def _keyword_stem_signature(keywords: list[str]) -> tuple[set[str], dict[str, int]]:
    unique: set[str] = set()
    counts: dict[str, int] = {}
    for keyword in keywords or []:
        stems = {
            _stem_token(token)
            for token in re.findall(r"[0-9a-zа-яё]+", str(keyword or ""), flags=re.IGNORECASE)
            if len(token) >= 3
        }
        for stem in stems:
            if not stem:
                continue
            unique.add(stem)
            counts[stem] = counts.get(stem, 0) + 1
    return unique, counts


def _sanitize_keyword_source_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    cleaned = re.sub(r"my main account is\s+@[0-9a-z._]+", " ", raw, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"send me stuff\s*:.*?(anything sent to the above address|contact\s*:|videos are for entertainment purposes only|attempt any repairs at your own risk|$)",
        " ",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"anything sent to the above address.*?(contact\s*:|videos are for entertainment purposes only|attempt any repairs at your own risk|$)",
        " ",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"contact\s*:.*?(videos are for entertainment purposes only|attempt any repairs at your own risk|$)",
        " ",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"videos are for entertainment purposes only.*?$",
        " ",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"attempt any repairs at your own risk.*?$",
        " ",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    segments = re.split(r"[\n\r]+|(?<=[.!?;])\s+", cleaned)
    kept: list[str] = []
    for segment in segments:
        value = str(segment or "").strip()
        if not value:
            continue
        if any(pattern.search(value) for pattern in _BOILERPLATE_SOURCE_PATTERNS):
            continue
        kept.append(value)
    return " ".join(kept).strip()


def _clean_niche_keywords(keywords: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        value = str(keyword or "").strip()
        if not value:
            continue
        key = value.lower()
        if len(key) < 3 or key in _NOISE_KEYWORDS or key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
    return cleaned[:12]


def _trim_keywords(keywords: list[str], limit: int = 6) -> list[str]:
    return _clean_niche_keywords(keywords)[:limit]


def _prioritize_keywords(*, primary: list[str], topic_phrases: list[str], secondary: list[str] | None = None) -> list[str]:
    secondary = list(secondary or [])
    combined = list(primary[:2]) + list(topic_phrases) + list(primary[2:]) + secondary
    return _trim_keywords(combined, limit=8)


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
    english_context = bool(re.search(r"(англий|english|language)", raw, flags=re.IGNORECASE))
    if english_context and _FOR_BEGINNERS_RE.search(raw):
        if "английский для начинающих" not in seen:
            seen.add("английский для начинающих")
            phrases.append("английский для начинающих")
    if english_context and _FOR_ADULTS_RE.search(raw):
        if "английский для взрослых" not in seen:
            seen.add("английский для взрослых")
            phrases.append("английский для взрослых")
    if english_context and _CONVERSATIONAL_RE.search(raw):
        if "разговорный английский" not in seen:
            seen.add("разговорный английский")
            phrases.append("разговорный английский")
    return phrases[:8]


def is_niche_keywords_poor(*, keywords: list[str], seed: SeedResolution | None = None) -> bool:
    kws = _trim_keywords(list(keywords or []), limit=12)
    if len(kws) < 4:
        return True

    phrase_count = sum(1 for keyword in kws if " " in keyword)
    cyr_count = sum(1 for keyword in kws if re.search(r"[а-яё]", keyword.lower()))
    if phrase_count >= 2 and len(kws) >= 5:
        return False

    if phrase_count == 0 and cyr_count <= 1:
        return True

    unique_stems, stem_counts = _keyword_stem_signature(kws)
    dominant = max(stem_counts.values(), default=0)
    if len(unique_stems) <= 5 and len(kws) <= 4:
        return True
    if dominant >= max(3, len(kws) - 1) and len(unique_stems) <= 6:
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


def _text_theme_stems(texts: list[str]) -> set[str]:
    stems: set[str] = set()
    for text in texts:
        cleaned_text = _sanitize_keyword_source_text(str(text or ""))
        if not cleaned_text:
            continue
        for raw in _ACCOUNT_TOKEN_RE.findall(cleaned_text):
            norm = _normalize_token(raw)
            if (
                len(norm) < 3
                or norm in _STOPWORDS_EN
                or norm in _STOPWORDS_RU
                or norm in _STRUCTURAL_JUNK
                or norm in _LOW_INFORMATION
            ):
                continue
            stem = _stem_token(norm)
            if not stem or stem in _UTILITY_JUNK_STEMS:
                continue
            stems.add(stem)
    return stems


def _account_theme_texts(
    *,
    account: SeedResolution,
    context: SetupRunContext | None = None,
) -> list[str]:
    texts = [
        str(account.title or "").strip(),
        str(account.description or "").strip(),
    ]
    texts.extend(_safe_recent_seed_content_texts(seed=account, n=8, context=context))
    return [text for text in texts if text]


def _same_handle_profile_hint_sources(
    *,
    seed: SeedResolution,
    linked_accounts: list[SeedResolution] | None,
) -> list[KeywordSource]:
    seed_handle = str(seed.handle or "").strip().lower()
    if not seed_handle:
        return []
    out: list[KeywordSource] = []
    seen: set[tuple[str, str]] = set()
    for account in _ordered_accounts(seed=seed, linked_accounts=linked_accounts)[1:]:
        account_handle = str(account.handle or "").strip().lower()
        if not account_handle or account_handle != seed_handle:
            continue
        source_id = _account_key(account)
        if account.description:
            _append_keyword_source(
                out,
                text=account.description,
                source_id=source_id,
                source_type="description",
                seen=seen,
            )
    return out


def _filter_keyword_inference_linked_accounts(
    *,
    seed: SeedResolution,
    linked_accounts: list[SeedResolution] | None,
    context: SetupRunContext | None = None,
) -> list[SeedResolution]:
    ordered = _ordered_accounts(seed=seed, linked_accounts=linked_accounts)
    if len(ordered) <= 1:
        return [account for account in ordered[1:]]
    seed_stems = _text_theme_stems(_account_theme_texts(account=seed, context=context))
    if len(seed_stems) < 3:
        return [account for account in ordered[1:]]

    kept: list[SeedResolution] = []
    for account in ordered[1:]:
        account_stems = _text_theme_stems(_account_theme_texts(account=account, context=context))
        if not account_stems:
            continue
        overlap = len(seed_stems & account_stems)
        if overlap >= 2:
            kept.append(account)
    return kept


def _append_unique(parts: list[str], value: str, seen: set[str]) -> None:
    text = _sanitize_keyword_source_text(str(value or ""))
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
    value = _sanitize_keyword_source_text(str(text or ""))
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


def _source_stem_counts(keyword_sources: list[KeywordSource] | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in keyword_sources or []:
        seen_in_source: set[str] = set()
        for raw in _ACCOUNT_TOKEN_RE.findall(str(source.text or "")):
            norm = _normalize_token(raw)
            if len(norm) < 3:
                continue
            stem = _stem_token(norm)
            if stem in seen_in_source:
                continue
            seen_in_source.add(stem)
            counts[stem] = counts.get(stem, 0) + 1
    return counts


def build_keyword_blocked_terms(
    *,
    seed: SeedResolution,
    linked_accounts: list[SeedResolution] | None = None,
    keyword_sources: list[KeywordSource] | None = None,
) -> set[str]:
    blocked: set[str] = set()
    source_stem_support = _source_stem_counts(keyword_sources)
    for account in _ordered_accounts(seed=seed, linked_accounts=linked_accounts):
        for token in re.findall(r"[0-9a-zа-яё]+", str(account.handle or ""), flags=re.IGNORECASE):
            norm = token.strip().lower().replace("ё", "е")
            stem = _stem_token(norm)
            if (
                len(norm) >= 3
                and stem not in _IDENTITY_SAFE_THEME_STEMS
                and source_stem_support.get(stem, 0) < 2
            ):
                blocked.add(norm)

        title_tokens = [
            token.strip().lower().replace("ё", "е")
            for token in re.findall(r"[0-9a-zа-яё]+", str(account.title or ""), flags=re.IGNORECASE)
            if len(token.strip()) >= 3
        ]
        unsupported_title_tokens = [
            token
            for token in title_tokens
            if _stem_token(token) not in _IDENTITY_SAFE_THEME_STEMS
            and source_stem_support.get(_stem_token(token), 0) < 2
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


def _build_title_keyword_sources(
    *,
    seed: SeedResolution,
    linked_accounts: list[SeedResolution] | None = None,
) -> list[KeywordSource]:
    sources: list[KeywordSource] = []
    seen: set[tuple[str, str]] = set()
    for account in _ordered_accounts(seed=seed, linked_accounts=linked_accounts):
        if account.title:
            _append_keyword_source(
                sources,
                text=account.title,
                source_id=_account_key(account),
                source_type="generic",
                seen=seen,
            )
    return sources


def _build_source_token_stats(keyword_sources: list[KeywordSource]) -> tuple[set[str], set[str], set[str]]:
    tokens: set[str] = set()
    stems: set[str] = set()
    digits: set[str] = set()
    for source in keyword_sources:
        for raw in _ACCOUNT_TOKEN_RE.findall(str(source.text or "")):
            norm = _normalize_token(raw)
            if not norm:
                continue
            if norm.isdigit():
                digits.add(norm)
                continue
            if (
                len(norm) < 3
                or norm in _STOPWORDS_EN
                or norm in _STOPWORDS_RU
                or norm in _STRUCTURAL_JUNK
                or norm in _LOW_INFORMATION
            ):
                continue
            tokens.add(norm)
            stems.add(_stem_token(norm))
    return tokens, stems, digits


def _candidate_subject_terms(
    *,
    seed: SeedResolution,
    linked_accounts: list[SeedResolution] | None,
    keywords: list[str],
    keyword_sources: list[KeywordSource],
) -> list[tuple[str, bool]]:
    source_tokens, source_stems, source_digits = _build_source_token_stats(keyword_sources)
    stem_support = _source_stem_counts(keyword_sources)
    out: list[tuple[str, bool]] = []
    seen: set[tuple[str, bool]] = set()

    def add(term: str, *, expandable: bool) -> None:
        value = _normalize_subject_term(term)
        if len(value) < 3:
            return
        stems = {_stem_token(token) for token in _ACCOUNT_TOKEN_RE.findall(value) if len(token) >= 3}
        if stems & _UTILITY_JUNK_STEMS and not stems & _IDENTITY_SAFE_THEME_STEMS:
            return
        key = (value, expandable)
        if key in seen:
            return
        seen.add(key)
        out.append((value, expandable))

    for account in _ordered_accounts(seed=seed, linked_accounts=linked_accounts):
        account_title = _normalize_token(account.title or "")
        for source_text in (str(account.title or ""), str(account.description or "")):
            for match in re.finditer(r"\b([0-9a-zа-яё-]{3,})\s*(\d{1,2})\b", source_text, flags=re.IGNORECASE):
                token = _normalize_token(match.group(1))
                number = match.group(2)
                if (
                    len(token) < 3
                    or token in _STOPWORDS_EN
                    or token in _STOPWORDS_RU
                    or token in _STRUCTURAL_JUNK
                    or token in _LOW_INFORMATION
                ):
                    continue
                stem = _stem_token(token)
                if stem in _UTILITY_JUNK_STEMS or stem in _GENERIC_SUBJECT_STEMS:
                    continue
                supported = (
                    stem in _IDENTITY_SAFE_THEME_STEMS
                    or stem_support.get(stem, 0) >= 2
                    or token in account_title
                )
                if not supported:
                    continue
                add(f"{token} {number}", expandable=True)

    for keyword in keywords:
        value = " ".join(str(keyword or "").split()).strip()
        if not value:
            continue
        if len(value.split()) == 1:
            stem = _stem_token(value)
            if stem not in _GENERIC_SUBJECT_STEMS and (
                stem in _IDENTITY_SAFE_THEME_STEMS or stem_support.get(stem, 0) >= 2
            ):
                add(value, expandable=True)

    for account in _ordered_accounts(seed=seed, linked_accounts=linked_accounts):
        handle_norm = re.sub(r"[^0-9a-zа-яё]+", "", _normalize_token(account.handle or ""))
        if handle_norm:
            for token in sorted(source_tokens, key=lambda item: (-len(item), item)):
                stem = _stem_token(token)
                if len(token) >= 4 and token in handle_norm and (
                    stem in _IDENTITY_SAFE_THEME_STEMS or stem_support.get(stem, 0) >= 2
                ):
                    add(token, expandable=True)

        for raw in _ACCOUNT_TOKEN_RE.findall(str(account.title or "")):
            norm = _normalize_token(raw)
            if len(norm) < 3:
                continue
            if raw.isupper():
                add(norm, expandable=False)
                if "2" in source_digits and not any(ch.isdigit() for ch in norm):
                    add(f"{norm} 2", expandable=False)

        for raw in _ACCOUNT_TOKEN_RE.findall(str(account.description or "")):
            norm = _normalize_token(raw)
            if len(norm) < 3 or norm.isdigit():
                continue
            stem = _stem_token(norm)
            if stem in source_stems and stem not in _GENERIC_SUBJECT_STEMS and (
                stem in _IDENTITY_SAFE_THEME_STEMS or stem_support.get(stem, 0) >= 2
            ):
                add(norm, expandable=True)

    return out[:4]


_GENERIC_SUBJECT_STEMS = {
    "group",
    "groups",
    "lesson",
    "online",
    "групп",
    "онлайн",
    "репетитор",
    "преподав",
    "преподавател",
    "урок",
    "учеб",
    "ученик",
    "учител",
    "школ",
}


def _normalize_subject_term(term: str) -> str:
    value = " ".join(str(term or "").split()).strip().lower()
    if not value:
        return ""
    if " " in value:
        return " ".join(_normalize_subject_term(part) for part in value.split() if part).strip()
    if value.endswith("ого") or value.endswith("ому"):
        return value[:-3] + "ий"
    if value.endswith("его") or value.endswith("ему"):
        return value[:-3] + "ий"
    return value


def _subject_object_form(term: str) -> str:
    value = _normalize_subject_term(term)
    if not value or " " in value:
        return value
    if value.endswith("ий"):
        return value[:-2] + "ого"
    if value.endswith("ый") or value.endswith("ой"):
        return value[:-2] + "ого"
    if value.endswith("ая"):
        return value[:-2] + "ой"
    if value.endswith("ое"):
        return value[:-2] + "ого"
    if value.endswith("а"):
        return value[:-1] + "ы"
    if value.endswith("я"):
        return value[:-1] + "и"
    return value


def _supplement_search_utility_keywords(
    *,
    seed: SeedResolution,
    linked_accounts: list[SeedResolution] | None,
    keywords: list[str],
    keyword_sources: list[KeywordSource],
) -> list[str]:
    subject_terms = _candidate_subject_terms(
        seed=seed,
        linked_accounts=linked_accounts,
        keywords=keywords,
        keyword_sources=keyword_sources,
    )
    if not subject_terms:
        return []

    source_text = "\n".join(str(source.text or "") for source in keyword_sources)
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        phrase = " ".join(str(value or "").split()).strip().lower()
        if len(phrase) < 3 or phrase in seen:
            return
        seen.add(phrase)
        out.append(phrase)

    for subject, expandable in subject_terms:
        add(subject)
        if not expandable:
            continue
        object_subject = _subject_object_form(subject)
        if _FOR_BEGINNERS_RE.search(source_text):
            add(f"{subject} для начинающих")
        if _FOR_ADULTS_RE.search(source_text):
            add(f"{subject} для взрослых")
        if _CONVERSATIONAL_RE.search(source_text):
            add(f"разговорный {subject}")
        if _LESSON_RE.search(source_text):
            add(f"уроки {object_subject}")
        if _SCHOOL_RE.search(source_text):
            add(f"школа {object_subject}")
            if "онлайн" in source_text.lower():
                add(f"онлайн школа {object_subject}")
        if _TEACHER_RE.search(source_text):
            add(f"преподаватель {object_subject}")
            add(f"репетитор {object_subject}")
        if _GUIDE_RE.search(source_text):
            add(f"гайды {subject}")
            add(f"разборы {subject}")
            if any(char.isdigit() for char in subject):
                add(f"патч {subject}")

    return out


def _merge_keyword_lists(*lists: list[str], max_keywords: int = 8) -> list[str]:
    merged: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for items in lists:
        for keyword in items:
            signature_tokens: set[str] = set()
            for raw in re.findall(r"[0-9a-zа-яё]+", keyword, flags=re.IGNORECASE):
                token = raw.strip().lower().replace("ё", "е")
                if not token:
                    continue
                if token.isdigit() and len(token) <= 2:
                    signature_tokens.add(f"n:{token}")
                    continue
                if len(token) >= 3:
                    signature_tokens.add(_stem_token(token))
            tokens = tuple(sorted(signature_tokens))
            if not tokens or tokens in seen:
                continue
            seen.add(tokens)
            merged.append(keyword)
            if len(merged) >= max_keywords:
                return merged
    return merged


def _filter_search_noise_keywords(keywords: list[str]) -> list[str]:
    filtered: list[str] = []
    for keyword in keywords:
        tokens = [_normalize_token(token) for token in re.findall(r"[0-9a-zа-яё]+", keyword, flags=re.IGNORECASE)]
        stems = {_stem_token(token) for token in tokens if len(token) >= 3}
        if not stems:
            continue
        useful = stems & _IDENTITY_SAFE_THEME_STEMS
        utility_hits = stems & _UTILITY_JUNK_STEMS
        if stems & _UTILITY_JUNK_STEMS and not useful and not any(token.isdigit() for token in tokens):
            continue
        if len(utility_hits) >= 2 and not useful:
            continue
        if tokens and _stem_token(tokens[0]) in _UTILITY_JUNK_STEMS and not useful:
            continue
        if len(tokens) == 1 and not useful and (stems & (_UTILITY_JUNK_STEMS | _GENERIC_SUBJECT_STEMS)):
            continue
        filtered.append(keyword)
    return filtered


def _drop_generic_singletons_with_richer_phrases(keywords: list[str]) -> list[str]:
    phrase_tokens = {
        token
        for keyword in keywords
        if " " in str(keyword or "").strip()
        for token in re.findall(r"[0-9a-zа-яё]+", keyword, flags=re.IGNORECASE)
        if len(token) >= 3
    }
    out: list[str] = []
    for keyword in keywords:
        if any(char.isdigit() for char in str(keyword or "")):
            out.append(keyword)
            continue
        tokens = [token for token in re.findall(r"[0-9a-zа-яё]+", keyword, flags=re.IGNORECASE) if len(token) >= 3]
        if len(tokens) == 1 and tokens[0] in phrase_tokens:
            continue
        out.append(keyword)
    return out


def _derive_supported_source_phrases(
    *,
    keyword_sources: list[KeywordSource],
    blocked_terms: list[str],
    existing_keywords: list[str],
    limit: int = 8,
) -> list[str]:
    blocked_tokens, blocked_stems = _normalize_blocked_terms(blocked_terms)
    phrase_support: dict[tuple[str, ...], dict[str, object]] = {}
    existing_signatures = {
        tuple(
            sorted(
                {
                    _stem_token(token)
                    for token in re.findall(r"[0-9a-zа-яё]+", str(keyword or ""), flags=re.IGNORECASE)
                    if len(token) >= 3
                }
            )
        )
        for keyword in existing_keywords or []
    }
    for source_index, source in enumerate(keyword_sources):
        source_id = str(source.source_id or f"source-{source_index}")
        for chunk_index, raw_segment in enumerate(_SOURCE_PHRASE_SPLIT_RE.split(str(source.text or "")), start=1):
            tokens = [_normalize_token(token) for token in _TOKEN_RE.findall(raw_segment)]
            if len(tokens) < 2:
                continue
            for start in range(len(tokens)):
                for size in (2, 3):
                    window = tokens[start : start + size]
                    if len(window) != size:
                        continue
                    if not all(
                        _is_content_token(token, blocked_tokens=blocked_tokens, blocked_stems=blocked_stems)
                        for token in window
                    ):
                        continue
                    signature = tuple(sorted(_stem_token(token) for token in window if len(token) >= 3))
                    if not signature or signature in existing_signatures:
                        continue
                    normalized_phrase = " ".join(window).strip().lower()
                    if not normalized_phrase:
                        continue
                    record = phrase_support.setdefault(
                        signature,
                        {
                            "display": normalized_phrase,
                            "source_ids": set(),
                            "chunk_ids": set(),
                            "length": size,
                            "count": 0,
                        },
                    )
                    record["source_ids"].add(source_id)
                    record["chunk_ids"].add(f"{source_id}:{chunk_index}")
                    record["count"] = int(record["count"]) + 1

    ranked = sorted(
        phrase_support.items(),
        key=lambda item: (
            -len(item[1]["source_ids"]),
            -len(item[1]["chunk_ids"]),
            -int(item[1]["count"]),
            int(item[1]["length"]),
            str(item[1]["display"]),
        ),
    )
    out: list[str] = []
    seen_stems: set[tuple[str, ...]] = set(existing_signatures)
    for signature, record in ranked:
        if signature in seen_stems:
            continue
        display = str(record["display"]).strip()
        if len(display.split()) < 2:
            continue
        seen_stems.add(signature)
        out.append(display)
        if len(out) >= limit:
            break
    return out


def _derive_game_topic_keywords(text: str) -> list[str]:
    raw = str(text or "").strip().lower()
    if not raw:
        return []
    if re.search(r"\b(dota\s*2|дота\s*2)\b", raw, flags=re.IGNORECASE):
        return ["dota 2", "dota"]
    if re.search(r"\b(dota|дота)\b", raw, flags=re.IGNORECASE):
        return ["dota"]
    return []


def infer_niche_keywords(
    *,
    seed: SeedResolution,
    competitors: list[SeedResolution],
    linked_accounts: list[SeedResolution] | None = None,
    context: SetupRunContext | None = None,
) -> tuple[list[str], str]:
    """
    Returns: (keywords, source) where source is always "auto".
    """
    filtered_linked_accounts = _filter_keyword_inference_linked_accounts(
        seed=seed,
        linked_accounts=linked_accounts,
        context=context,
    )
    fallback_text = build_keyword_source_text(
        seed=seed,
        competitors=competitors,
        linked_accounts=filtered_linked_accounts,
        context=context,
    )
    topic_phrases = _derive_topic_phrases(fallback_text)
    game_topic_keywords = _derive_game_topic_keywords(fallback_text)
    keyword_sources = build_keyword_sources(
        seed=seed,
        competitors=competitors,
        linked_accounts=filtered_linked_accounts,
        context=context,
    )
    keyword_sources.extend(_same_handle_profile_hint_sources(seed=seed, linked_accounts=linked_accounts))
    blocked_terms = build_keyword_blocked_terms(
        seed=seed,
        linked_accounts=linked_accounts,
        keyword_sources=keyword_sources,
    )
    auto_keywords = extract_keywords(
        keyword_sources,
        max_keywords=8,
        blocked_terms=blocked_terms,
    )
    auto_keywords = _supplement_exam_subject_keywords(keywords=auto_keywords, keyword_sources=keyword_sources)[:8]
    auto_keywords = _filter_search_noise_keywords(auto_keywords)
    auto_keywords = _merge_keyword_lists(
        _supplement_search_utility_keywords(
            seed=seed,
            linked_accounts=linked_accounts,
            keywords=auto_keywords,
            keyword_sources=keyword_sources,
        ),
        auto_keywords,
        max_keywords=8,
    )
    title_keywords = extract_keywords(
        _build_title_keyword_sources(seed=seed, linked_accounts=filtered_linked_accounts),
        max_keywords=3,
        blocked_terms=blocked_terms,
    )
    auto_keywords = _merge_keyword_lists(auto_keywords, title_keywords, max_keywords=8)
    supported_source_phrases = _derive_supported_source_phrases(
        keyword_sources=keyword_sources,
        blocked_terms=blocked_terms,
        existing_keywords=auto_keywords,
        limit=8,
    )
    compact_source_phrases = [phrase for phrase in supported_source_phrases if len(str(phrase).split()) <= 3]
    if seed.platform == "youtube":
        try:
            youtube_keywords = infer_youtube_keywords(seed)
        except Exception as exc:
            logger.warning("YouTube keyword inference failed for %s:%s: %s", seed.platform, seed.external_id, exc)
            youtube_keywords = []
        auto_keywords = _merge_keyword_lists(
            topic_phrases,
            youtube_keywords,
            auto_keywords,
            supported_source_phrases,
            max_keywords=8,
        )
    else:
        auto_keywords = _merge_keyword_lists(topic_phrases, auto_keywords, supported_source_phrases, max_keywords=8)
    auto_keywords = _merge_keyword_lists(game_topic_keywords, auto_keywords, max_keywords=8)
    auto_keywords = _prioritize_keywords(primary=auto_keywords, topic_phrases=topic_phrases)
    if is_niche_keywords_poor(keywords=auto_keywords, seed=seed):
        auto_keywords = _merge_keyword_lists(auto_keywords, compact_source_phrases, supported_source_phrases, max_keywords=8)
    auto_keywords = _filter_search_noise_keywords(auto_keywords)
    auto_keywords = _drop_generic_singletons_with_richer_phrases(auto_keywords)[:8]
    if len(auto_keywords) < 5 or is_niche_keywords_poor(keywords=auto_keywords, seed=seed):
        auto_keywords = _merge_keyword_lists(auto_keywords, compact_source_phrases, supported_source_phrases, max_keywords=8)
        auto_keywords = _filter_search_noise_keywords(auto_keywords)
        auto_keywords = _drop_generic_singletons_with_richer_phrases(auto_keywords)[:8]
    return auto_keywords, "auto"
