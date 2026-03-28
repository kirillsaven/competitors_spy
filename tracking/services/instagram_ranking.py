from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from tracking.services.discovery_profiles import (
    ARCHETYPE_CLINIC,
    ARCHETYPE_COMMERCE,
    ARCHETYPE_INSTITUTION,
    ENTITY_CREATOR_EDUCATIONAL,
    ENTITY_CREATOR_PERSONAL,
    ENTITY_INSTITUTION_CLINIC,
    ENTITY_INSTITUTION_SCHOOL,
    classify_entity_profile,
    infer_archetype_profile,
)
from tracking.services.instagram_intent import DiscoveryIntent


_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_SPAM_HANDLE_RE = re.compile(r"(_.*_.*)|(\d{2,})")
_COMMERCE_MARKERS = {"shop", "store", "beauty", "jewel", "cosmetic", "boutique"}


@dataclass(frozen=True)
class CandidateScores:
    entity_match: float
    topic_match: float
    graph_score: float
    archetype_match: float
    penalty: float
    total: float
    entity_type_guess: str
    archetype_guess: str


def _joined_text(candidate: Any, texts: list[str] | None = None) -> str:
    parts = [
        str(getattr(candidate, "handle", "") or "").strip(),
        str(getattr(candidate, "display_name", "") or "").strip(),
        str(getattr(candidate, "description", "") or "").strip(),
    ]
    parts.extend(str(text or "").strip() for text in (texts or []) if str(text or "").strip())
    return "\n".join(part for part in parts if part)


def _token_overlap(text: str, terms: list[str]) -> int:
    if not text or not terms:
        return 0
    lowered = text.lower()
    return sum(1 for term in terms if str(term or "").strip() and str(term).lower() in lowered)


def compute_entity_scores(candidate: Any, intent: DiscoveryIntent, *, texts: list[str] | None = None) -> dict[str, float | str]:
    profile = classify_entity_profile(
        handle=getattr(candidate, "handle", None),
        title=getattr(candidate, "display_name", None),
        description=getattr(candidate, "description", None),
        recent_texts=texts,
    )
    match = 0.0
    if intent.entity_type == profile.entity_type:
        match = 6.0
    elif intent.entity_type in {ENTITY_CREATOR_PERSONAL, ENTITY_CREATOR_EDUCATIONAL} and profile.entity_type in {ENTITY_CREATOR_PERSONAL, ENTITY_CREATOR_EDUCATIONAL}:
        match = 4.5
    elif intent.entity_type in {ENTITY_INSTITUTION_CLINIC, ENTITY_INSTITUTION_SCHOOL} and profile.entity_type in {ENTITY_INSTITUTION_CLINIC, ENTITY_INSTITUTION_SCHOOL}:
        match = 4.0
    else:
        match = max(0.0, profile.creator_likeness_score - profile.institution_likeness_score)
    return {
        "entity_type_guess": profile.entity_type,
        "entity_confidence": profile.confidence,
        "creator_likeness_score": profile.creator_likeness_score,
        "institution_likeness_score": profile.institution_likeness_score,
        "entity_match": match,
    }


def compute_topic_scores(candidate: Any, intent: DiscoveryIntent, *, texts: list[str] | None = None) -> dict[str, float | str]:
    joined = _joined_text(candidate, texts=texts)
    topic_overlap = _token_overlap(joined, intent.topic_terms)
    creator_overlap = _token_overlap(joined, intent.creator_terms)
    institution_overlap = _token_overlap(joined, intent.institution_terms)
    format_overlap = _token_overlap(joined, intent.format_terms)
    archetype = infer_archetype_profile(
        title=getattr(candidate, "display_name", None),
        description=getattr(candidate, "description", None),
        recent_texts=texts,
        entity_profile=None,
    )
    archetype_match = 3.0 if archetype.archetype == intent.archetype_profile.archetype and archetype.archetype != "unknown" else 0.0
    return {
        "topic_match": float(topic_overlap * 2.5 + creator_overlap * 2.0 + institution_overlap * 1.5 + format_overlap * 1.8),
        "topic_overlap": float(topic_overlap),
        "creator_overlap": float(creator_overlap),
        "institution_overlap": float(institution_overlap),
        "format_overlap": float(format_overlap),
        "archetype_guess": archetype.archetype,
        "archetype_match": archetype_match,
    }


def compute_graph_scores(candidate: Any, intent: DiscoveryIntent) -> dict[str, float]:
    metadata = getattr(candidate, "metadata", {}) or {}
    raw_distance = metadata.get("graph_distance") or metadata.get("graph_depth")
    distance = max(1, int(raw_distance)) if raw_distance else 4
    support = max(0, int(metadata.get("graph_support_count") or metadata.get("graph_hits") or 0))
    source = str(metadata.get("retrieval_source") or metadata.get("source") or "").strip().lower()
    score = float(max(0, 5 - distance) + min(4, support * 1.5))
    if source in {"seed_related", "linked_related", "cross_platform", "expanded_related", "related_profile"}:
        score += 2.0
    if source == "cross_platform":
        score += 1.5
    return {"graph_score": score}


def compute_penalties(candidate: Any, intent: DiscoveryIntent, *, texts: list[str] | None = None) -> dict[str, float]:
    metadata = getattr(candidate, "metadata", {}) or {}
    handle = str(getattr(candidate, "handle", "") or "")
    joined = _joined_text(candidate, texts=texts).lower()
    entity_type_guess = str(metadata.get("entity_type_guess") or "")
    penalty = 0.0
    if _SPAM_HANDLE_RE.search(handle):
        penalty += 7.0
    if any(marker in handle.lower() for marker in _COMMERCE_MARKERS):
        penalty += 5.0
    if intent.entity_type in {ENTITY_CREATOR_PERSONAL, ENTITY_CREATOR_EDUCATIONAL} and entity_type_guess in {ENTITY_INSTITUTION_CLINIC, ENTITY_INSTITUTION_SCHOOL}:
        penalty += 8.0
    if intent.entity_type in {ENTITY_INSTITUTION_CLINIC, ENTITY_INSTITUTION_SCHOOL} and entity_type_guess in {ENTITY_CREATOR_PERSONAL, ENTITY_CREATOR_EDUCATIONAL}:
        penalty += 4.0
    if any(term.lower() in joined for term in intent.negative_terms):
        penalty += 3.0
    if str(metadata.get("retrieval_source") or "") == "search" and not metadata.get("graph_support_count"):
        penalty += 1.5
    return {"penalty": penalty}


def candidate_passes_entity_gate(candidate: Any, intent: DiscoveryIntent, *, entity_scores: dict[str, float | str]) -> bool:
    guess = str(entity_scores.get("entity_type_guess") or "")
    if intent.entity_type in {ENTITY_CREATOR_PERSONAL, ENTITY_CREATOR_EDUCATIONAL}:
        return guess not in {ENTITY_INSTITUTION_CLINIC, ENTITY_INSTITUTION_SCHOOL}
    if intent.entity_type in {ENTITY_INSTITUTION_CLINIC, ENTITY_INSTITUTION_SCHOOL}:
        return guess not in {ENTITY_CREATOR_PERSONAL, ENTITY_CREATOR_EDUCATIONAL}
    return True


def candidate_passes_topic_gate(candidate: Any, intent: DiscoveryIntent, *, topic_scores: dict[str, float | str]) -> bool:
    if float(topic_scores.get("topic_overlap") or 0) >= 1:
        return True
    if float(topic_scores.get("creator_overlap") or 0) >= 1 and intent.entity_type in {ENTITY_CREATOR_PERSONAL, ENTITY_CREATOR_EDUCATIONAL}:
        return True
    if float(topic_scores.get("institution_overlap") or 0) >= 1 and intent.entity_type in {ENTITY_INSTITUTION_CLINIC, ENTITY_INSTITUTION_SCHOOL}:
        return True
    if float(topic_scores.get("format_overlap") or 0) >= 2:
        return True
    metadata = getattr(candidate, "metadata", {}) or {}
    if float(topic_scores.get("archetype_match") or 0) >= 3.0 and float(metadata.get("graph_score") or 0) >= 5.0:
        return True
    if float(metadata.get("graph_support_count") or metadata.get("graph_hits") or 0) >= 2 and float(topic_scores.get("creator_overlap") or 0) >= 1:
        return True
    return False


def candidate_passes_activity_gate(candidate: Any) -> bool:
    metadata = getattr(candidate, "metadata", {}) or {}
    return int(metadata.get("recent_collectible_count") or 0) >= 1


def score_instagram_candidate(candidate: Any, intent: DiscoveryIntent, *, texts: list[str] | None = None) -> CandidateScores:
    entity_scores = compute_entity_scores(candidate, intent, texts=texts)
    metadata = getattr(candidate, "metadata", {}) or {}
    metadata.update(entity_scores)
    topic_scores = compute_topic_scores(candidate, intent, texts=texts)
    metadata.update(topic_scores)
    graph_scores = compute_graph_scores(candidate, intent)
    metadata.update(graph_scores)
    penalties = compute_penalties(candidate, intent, texts=texts)
    metadata.update(penalties)
    total = (
        float(entity_scores.get("entity_match") or 0)
        + float(topic_scores.get("topic_match") or 0)
        + float(topic_scores.get("archetype_match") or 0)
        + float(graph_scores.get("graph_score") or 0)
        - float(penalties.get("penalty") or 0)
    )
    return CandidateScores(
        entity_match=float(entity_scores.get("entity_match") or 0),
        topic_match=float(topic_scores.get("topic_match") or 0),
        graph_score=float(graph_scores.get("graph_score") or 0),
        archetype_match=float(topic_scores.get("archetype_match") or 0),
        penalty=float(penalties.get("penalty") or 0),
        total=total,
        entity_type_guess=str(entity_scores.get("entity_type_guess") or ""),
        archetype_guess=str(topic_scores.get("archetype_guess") or ""),
    )
