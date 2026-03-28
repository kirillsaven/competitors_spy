from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Iterable

from common.text import KeywordSource, extract_keywords
from tracking.adapters.base import SeedResolution


ENTITY_CREATOR_PERSONAL = "creator_personal"
ENTITY_CREATOR_EDUCATIONAL = "creator_educational"
ENTITY_BRAND_MEDIA = "brand_media"
ENTITY_INSTITUTION_SCHOOL = "institution_school"
ENTITY_INSTITUTION_CLINIC = "institution_clinic"
ENTITY_COMMUNITY_AGGREGATOR = "community_aggregator"
ENTITY_UNKNOWN = "unknown"

ARCHETYPE_EXPLAINER = "explainer"
ARCHETYPE_REACTS = "reacts_commentary"
ARCHETYPE_TUTORIAL = "tutorial_teacher"
ARCHETYPE_EXPERIMENT = "experiment_demo"
ARCHETYPE_LIFESTYLE = "personal_lifestyle"
ARCHETYPE_INSTITUTION = "institutional_education"
ARCHETYPE_CLINIC = "clinic_practice"
ARCHETYPE_COMMERCE = "ecommerce_showcase"
ARCHETYPE_UNKNOWN = "unknown"

_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_PERSONAL_MARKERS = {
    "i", "im", "i'm", "me", "my", "doctor", "dr", "host", "creator", "reacts", "reviews",
    "меня", "мой", "моя", "мои", "я", "личный",
}
_EDUCATIONAL_MARKERS = {
    "teacher", "tutor", "lesson", "lessons", "explainer", "explained", "education", "tutorial",
    "learn", "teaching", "professor", "chemist", "scientist", "engineer", "doctor", "medicine",
    "репетитор", "урок", "уроки", "обучение", "объясняю", "разбор", "гайды", "разборы",
}
_INSTITUTION_SCHOOL_MARKERS = {
    "academy", "college", "course", "courses", "department", "faculty", "institute", "institution",
    "program", "programs", "residency", "school", "schools", "university", "training", "foundation",
    "trust", "campus", "official", "class", "classes",
    "академия", "кафедра", "университет", "школа", "школы", "курс", "курсы", "факультет",
}
_INSTITUTION_CLINIC_MARKERS = {
    "care", "center", "centre", "clinic", "doctor's", "doctors", "family medicine", "hospital",
    "medical center", "medical group", "residency", "health system", "practice", "residency",
    "клиника", "больница", "медицина", "медицинский центр",
}
_MEDIA_MARKERS = {
    "magazine", "media", "network", "news", "podcast", "press", "radio", "show", "studio",
}
_AGGREGATOR_MARKERS = {
    "daily", "hub", "community", "communities", "group", "groups", "tips", "quotes", "memes",
    "facts", "news", "updates", "digest", "highlights", "compilation",
}
_COMMERCE_MARKERS = {
    "beauty", "buy", "boutique", "candles", "cosmetics", "discount", "drop", "jewels", "jewellery",
    "jewelry", "official store", "order", "orders", "price", "prices", "sale", "shop", "skincare",
    "store", "stores",
}
_EXPLAINER_MARKERS = {"explainer", "explained", "why", "how", "understand", "understanding", "science", "chemistry"}
_REACTS_MARKERS = {"reacts", "reaction", "responds", "commentary", "review", "reviews"}
_TUTORIAL_MARKERS = {"lesson", "lessons", "tutorial", "tutorials", "teacher", "tutor", "tips", "guide", "guides"}
_EXPERIMENT_MARKERS = {"experiment", "experiments", "demo", "diy", "build", "built", "making", "maker", "lab"}
_LIFESTYLE_MARKERS = {"vlog", "lifestyle", "day in the life", "morning", "routine", "travel"}
_CLINIC_MARKERS = {"clinic", "hospital", "patient", "residency", "surgery", "practice"}
_INSTITUTIONAL_MARKERS = _INSTITUTION_SCHOOL_MARKERS | {"curriculum", "seminar", "conference", "workshop"}
_STOP_TOKENS = {
    "and", "for", "from", "into", "that", "this", "with", "your", "our", "you", "the", "are",
    "как", "для", "это", "эти", "или", "что", "где", "про", "they", "their", "them",
}


@dataclass(frozen=True)
class TopicProfile:
    terms: list[str]
    source_terms: list[str]
    creator_terms: list[str]
    institution_terms: list[str]
    format_terms: list[str]
    negative_terms: list[str]


@dataclass(frozen=True)
class EntityProfile:
    entity_type: str
    confidence: float
    creator_likeness_score: float
    institution_likeness_score: float
    brand_media_score: float
    community_score: float
    commerce_score: float
    matched_markers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ArchetypeProfile:
    archetype: str
    confidence: float
    matched_markers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DiscoveryDiagnostics:
    seed_entity_type: str
    queries_used: list[str]
    raw_pool_count: int
    graph_pool_count: int
    search_pool_count: int
    post_entity_gate_count: int
    post_topic_gate_count: int
    final_count: int
    entity_breakdown_top20: dict[str, int] = field(default_factory=dict)
    retrieval_breakdown_top20: dict[str, int] = field(default_factory=dict)
    drop_reasons: dict[str, int] = field(default_factory=dict)
    candidate_debug: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _normalize_text(value: str | None) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _contains_any(text: str, markers: Iterable[str]) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []
    hits: list[str] = []
    for marker in markers:
        token = str(marker or "").strip().lower()
        if not token:
            continue
        if " " in token:
            if token in normalized:
                hits.append(token)
            continue
        if re.search(rf"(?<![0-9a-zа-яё]){re.escape(token)}(?![0-9a-zа-яё])", normalized, re.IGNORECASE):
            hits.append(token)
    return hits


def _token_terms(texts: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for raw in _TOKEN_RE.findall(str(text or "").lower()):
            if len(raw) < 4 or raw in _STOP_TOKENS or raw in seen:
                continue
            seen.add(raw)
            out.append(raw)
    return out


def classify_entity_profile(
    *,
    handle: str | None,
    title: str | None,
    description: str | None,
    recent_texts: list[str] | None = None,
) -> EntityProfile:
    recent_texts = list(recent_texts or [])
    joined = "\n".join(part for part in [handle or "", title or "", description or "", *recent_texts[:8]] if part).strip()
    personal_hits = _contains_any(joined, _PERSONAL_MARKERS)
    educational_hits = _contains_any(joined, _EDUCATIONAL_MARKERS)
    school_hits = _contains_any(joined, _INSTITUTION_SCHOOL_MARKERS)
    clinic_hits = _contains_any(joined, _INSTITUTION_CLINIC_MARKERS)
    media_hits = _contains_any(joined, _MEDIA_MARKERS)
    community_hits = _contains_any(joined, _AGGREGATOR_MARKERS)
    commerce_hits = _contains_any(joined, _COMMERCE_MARKERS)

    creator_score = float(len(personal_hits) * 2 + len(educational_hits) * 1.5 + sum(1 for text in recent_texts if len(text) >= 20))
    institution_score = float(len(school_hits) * 2.2 + len(clinic_hits) * 2.5)
    brand_media_score = float(len(media_hits) * 2)
    community_score = float(len(community_hits) * 1.8)
    commerce_score = float(len(commerce_hits) * 2.2)

    if commerce_score >= max(4.0, creator_score + 2, institution_score + 2):
        entity_type = ENTITY_COMMUNITY_AGGREGATOR
    elif clinic_hits:
        clinic_score = institution_score + len(clinic_hits)
        entity_type = ENTITY_INSTITUTION_CLINIC if clinic_score >= creator_score + 1.5 else ENTITY_CREATOR_EDUCATIONAL
    elif school_hits and institution_score >= creator_score + 1.5:
        entity_type = ENTITY_INSTITUTION_SCHOOL
    elif creator_score >= max(3.0, institution_score, brand_media_score, community_score, commerce_score):
        entity_type = ENTITY_CREATOR_EDUCATIONAL if educational_hits else ENTITY_CREATOR_PERSONAL
    elif brand_media_score >= max(2.5, creator_score + 0.5, institution_score + 0.5):
        entity_type = ENTITY_BRAND_MEDIA
    elif community_score >= max(2.5, creator_score + 0.5, institution_score + 0.5):
        entity_type = ENTITY_COMMUNITY_AGGREGATOR
    else:
        entity_type = ENTITY_UNKNOWN

    all_scores = [creator_score, institution_score, brand_media_score, community_score, commerce_score, 1.0]
    confidence = min(1.0, max(all_scores) / (sum(sorted(all_scores, reverse=True)[:2]) or 1.0))
    matched_markers = list(dict.fromkeys(personal_hits + educational_hits + school_hits + clinic_hits + media_hits + community_hits + commerce_hits))
    return EntityProfile(
        entity_type=entity_type,
        confidence=confidence,
        creator_likeness_score=creator_score,
        institution_likeness_score=institution_score,
        brand_media_score=brand_media_score,
        community_score=community_score,
        commerce_score=commerce_score,
        matched_markers=matched_markers[:12],
    )


def infer_archetype_profile(
    *,
    title: str | None,
    description: str | None,
    recent_texts: list[str] | None = None,
    entity_profile: EntityProfile | None = None,
) -> ArchetypeProfile:
    recent_texts = list(recent_texts or [])
    joined = "\n".join(part for part in [title or "", description or "", *recent_texts[:8]] if part).strip()
    marker_sets = {
        ARCHETYPE_EXPLAINER: _EXPLAINER_MARKERS,
        ARCHETYPE_REACTS: _REACTS_MARKERS,
        ARCHETYPE_TUTORIAL: _TUTORIAL_MARKERS,
        ARCHETYPE_EXPERIMENT: _EXPERIMENT_MARKERS,
        ARCHETYPE_LIFESTYLE: _LIFESTYLE_MARKERS,
        ARCHETYPE_CLINIC: _CLINIC_MARKERS,
        ARCHETYPE_INSTITUTION: _INSTITUTIONAL_MARKERS,
        ARCHETYPE_COMMERCE: _COMMERCE_MARKERS,
    }
    scored: list[tuple[float, str, list[str]]] = []
    for archetype, markers in marker_sets.items():
        hits = _contains_any(joined, markers)
        score = float(len(hits))
        if archetype == ARCHETYPE_TUTORIAL and entity_profile and entity_profile.entity_type == ENTITY_CREATOR_EDUCATIONAL:
            score += 1.5
        if archetype == ARCHETYPE_INSTITUTION and entity_profile and entity_profile.entity_type in {ENTITY_INSTITUTION_CLINIC, ENTITY_INSTITUTION_SCHOOL}:
            score += 1.5
        if archetype == ARCHETYPE_CLINIC and entity_profile and entity_profile.entity_type == ENTITY_INSTITUTION_CLINIC:
            score += 1.5
        if archetype == ARCHETYPE_REACTS and any("react" in text.lower() for text in recent_texts):
            score += 1.0
        scored.append((score, archetype, hits))
    best_score, best_type, best_hits = max(scored, key=lambda item: (item[0], item[1]))
    if best_score <= 0:
        return ArchetypeProfile(archetype=ARCHETYPE_UNKNOWN, confidence=0.0, matched_markers=[])
    confidence = min(1.0, best_score / 5.0)
    return ArchetypeProfile(archetype=best_type, confidence=confidence, matched_markers=best_hits[:8])


def build_topic_profile(
    *,
    seed: SeedResolution | None,
    linked_accounts: list[SeedResolution] | None = None,
    recent_texts: list[str] | None = None,
    fallback_keywords: list[str] | None = None,
) -> TopicProfile:
    sources: list[KeywordSource] = []
    blocked_terms: set[str] = set()
    normalized_fallback_keywords = [
        " ".join(str(keyword or "").split()).strip()
        for keyword in list(fallback_keywords or [])[:8]
        if " ".join(str(keyword or "").split()).strip()
    ]
    for account in [seed] + list(linked_accounts or []):
        if not account:
            continue
        if account.handle:
            blocked_terms.add(str(account.handle).lower())
        if account.description:
            sources.append(KeywordSource(text=account.description, source_id=str(account.external_id), source_type="description"))
        if account.title:
            sources.append(KeywordSource(text=account.title, source_id=str(account.external_id), source_type="title"))
    for index, text in enumerate(list(recent_texts or [])[:12], start=1):
        sources.append(KeywordSource(text=text, source_id=f"recent-{index}", source_type="recent"))
    for keyword in normalized_fallback_keywords:
        sources.append(KeywordSource(text=keyword, source_id=f"fallback-{keyword}", source_type="generic"))
    extracted = extract_keywords(sources, max_keywords=8, blocked_terms=blocked_terms)
    source_terms = [term for term in extracted if len(term.split()) <= 4]
    merged_source_terms: list[str] = []
    seen_terms: set[str] = set()
    for term in source_terms + [keyword for keyword in normalized_fallback_keywords if len(keyword.split()) <= 4]:
        lowered = term.lower()
        if lowered in seen_terms:
            continue
        seen_terms.add(lowered)
        merged_source_terms.append(term)
    source_terms = merged_source_terms[:8]
    creator_terms = [term for term in source_terms if not _contains_any(term, _INSTITUTION_SCHOOL_MARKERS | _INSTITUTION_CLINIC_MARKERS)]
    institution_terms = [term for term in source_terms if _contains_any(term, _INSTITUTION_SCHOOL_MARKERS | _INSTITUTION_CLINIC_MARKERS)]
    format_terms = [term for term in source_terms if _contains_any(term, _REACTS_MARKERS | _TUTORIAL_MARKERS | _EXPERIMENT_MARKERS | _EXPLAINER_MARKERS)]
    negative_terms = []
    for term in source_terms:
        if _contains_any(term, _COMMERCE_MARKERS):
            negative_terms.append(term)
    if not source_terms:
        source_terms = _token_terms([account.description or "" for account in [seed] + list(linked_accounts or []) if account])[:6]
    return TopicProfile(
        terms=source_terms[:8],
        source_terms=source_terms[:8],
        creator_terms=creator_terms[:4],
        institution_terms=institution_terms[:4],
        format_terms=format_terms[:4],
        negative_terms=list(dict.fromkeys(negative_terms))[:4],
    )
