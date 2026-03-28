from __future__ import annotations

from dataclasses import dataclass, field

from tracking.adapters.base import SeedResolution
from tracking.services.discovery_profiles import (
    ENTITY_CREATOR_EDUCATIONAL,
    ENTITY_CREATOR_PERSONAL,
    ENTITY_INSTITUTION_CLINIC,
    ENTITY_INSTITUTION_SCHOOL,
    ArchetypeProfile,
    EntityProfile,
    TopicProfile,
    build_topic_profile,
    classify_entity_profile,
    infer_archetype_profile,
)


@dataclass(frozen=True)
class DiscoveryIntent:
    topic_terms: list[str]
    creator_terms: list[str]
    institution_terms: list[str]
    format_terms: list[str]
    negative_terms: list[str]
    entity_type: str
    seed_handle: str | None
    linked_handles: list[str]
    seed_cross_platform_titles: list[str]
    entity_profile: EntityProfile
    archetype_profile: ArchetypeProfile
    topic_profile: TopicProfile
    query_classes: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "topic_terms": list(self.topic_terms),
            "creator_terms": list(self.creator_terms),
            "institution_terms": list(self.institution_terms),
            "format_terms": list(self.format_terms),
            "negative_terms": list(self.negative_terms),
            "entity_type": self.entity_type,
            "seed_handle": self.seed_handle,
            "linked_handles": list(self.linked_handles),
            "seed_cross_platform_titles": list(self.seed_cross_platform_titles),
            "entity_profile": {
                "entity_type": self.entity_profile.entity_type,
                "confidence": self.entity_profile.confidence,
                "creator_likeness_score": self.entity_profile.creator_likeness_score,
                "institution_likeness_score": self.entity_profile.institution_likeness_score,
                "brand_media_score": self.entity_profile.brand_media_score,
                "community_score": self.entity_profile.community_score,
                "commerce_score": self.entity_profile.commerce_score,
                "matched_markers": list(self.entity_profile.matched_markers),
            },
            "archetype_profile": {
                "archetype": self.archetype_profile.archetype,
                "confidence": self.archetype_profile.confidence,
                "matched_markers": list(self.archetype_profile.matched_markers),
            },
            "topic_profile": {
                "terms": list(self.topic_profile.terms),
                "creator_terms": list(self.topic_profile.creator_terms),
                "institution_terms": list(self.topic_profile.institution_terms),
                "format_terms": list(self.topic_profile.format_terms),
                "negative_terms": list(self.topic_profile.negative_terms),
            },
            "query_classes": dict(self.query_classes),
        }


def build_instagram_discovery_intent(
    *,
    seed: SeedResolution | None,
    linked_accounts: list[SeedResolution] | None,
    fallback_keywords: list[str] | None = None,
    recent_texts: list[str] | None = None,
) -> DiscoveryIntent:
    linked_accounts = list(linked_accounts or [])
    titles = [str(account.title or "").strip() for account in [seed] + linked_accounts if account and str(account.title or "").strip()]
    handles = [str(account.handle or "").strip().lower() for account in linked_accounts if account and str(account.handle or "").strip()]
    entity_profile = classify_entity_profile(
        handle=seed.handle if seed else None,
        title=seed.title if seed else None,
        description=seed.description if seed else None,
        recent_texts=recent_texts,
    )
    archetype_profile = infer_archetype_profile(
        title=seed.title if seed else None,
        description=seed.description if seed else None,
        recent_texts=recent_texts,
        entity_profile=entity_profile,
    )
    topic_profile = build_topic_profile(
        seed=seed,
        linked_accounts=linked_accounts,
        recent_texts=recent_texts,
        fallback_keywords=fallback_keywords,
    )
    creator_terms = list(topic_profile.creator_terms)
    institution_terms = list(topic_profile.institution_terms)
    format_terms = list(topic_profile.format_terms)
    topic_terms = list(topic_profile.terms)
    negative_terms = list(topic_profile.negative_terms)

    if entity_profile.entity_type in {ENTITY_CREATOR_PERSONAL, ENTITY_CREATOR_EDUCATIONAL}:
        creator_terms = creator_terms or topic_terms[:3]
        negative_terms = list(dict.fromkeys(negative_terms + institution_terms))[:6]
    elif entity_profile.entity_type in {ENTITY_INSTITUTION_SCHOOL, ENTITY_INSTITUTION_CLINIC}:
        institution_terms = institution_terms or topic_terms[:3]
    return DiscoveryIntent(
        topic_terms=topic_terms[:6],
        creator_terms=creator_terms[:4],
        institution_terms=institution_terms[:4],
        format_terms=format_terms[:4],
        negative_terms=negative_terms[:6],
        entity_type=entity_profile.entity_type,
        seed_handle=str(seed.handle or "").strip().lower() or None if seed else None,
        linked_handles=list(dict.fromkeys(handle for handle in handles if handle))[:6],
        seed_cross_platform_titles=list(dict.fromkeys(title for title in titles if title))[:6],
        entity_profile=entity_profile,
        archetype_profile=archetype_profile,
        topic_profile=topic_profile,
        query_classes={},
    )


def build_instagram_queries(intent: DiscoveryIntent) -> list[str]:
    queries: list[tuple[str, str]] = []

    def add(query: str, query_class: str) -> None:
        normalized = " ".join(str(query or "").split()).strip()
        if len(normalized) < 3:
            return
        if len(normalized.split()) > 4:
            return
        lowered = normalized.lower()
        if any(term and term.lower() == lowered for term in intent.negative_terms):
            return
        if lowered in {item[0].lower() for item in queries}:
            return
        queries.append((normalized, query_class))

    def add_variant_terms(terms: list[str], query_class: str) -> None:
        replacements = {
            " teachers": " tutor",
            " teacher": " tutor",
            " lessons": " tutorial",
            " lesson": " tutorial",
        }
        for term in terms:
            lowered = f" {term.lower()} "
            for source, target in replacements.items():
                if source in lowered:
                    add(term.lower().replace(source.strip(), target.strip()), query_class)
                    break

    if intent.entity_type in {ENTITY_CREATOR_PERSONAL, ENTITY_CREATOR_EDUCATIONAL}:
        for term in intent.creator_terms[:2]:
            add(term, "creator")
        for term in intent.format_terms[:2]:
            add(term, "format")
        for term in intent.topic_terms[:2]:
            add(term, "topic")
        add_variant_terms(intent.creator_terms[:2] + intent.topic_terms[:2], "creator")
    elif intent.entity_type in {ENTITY_INSTITUTION_SCHOOL, ENTITY_INSTITUTION_CLINIC}:
        for term in intent.institution_terms[:2]:
            add(term, "institution")
        for term in intent.topic_terms[:2]:
            add(term, "topic")
    else:
        for term in intent.topic_terms[:3]:
            add(term, "topic")
        for term in intent.creator_terms[:2]:
            add(term, "creator")
        add_variant_terms(intent.topic_terms[:3] + intent.creator_terms[:2], "creator")

    if not queries:
        for term in intent.topic_terms[:4]:
            add(term, "topic")
    intent.query_classes.update({query: query_class for query, query_class in queries})
    return [query for query, _query_class in queries[:5]]
