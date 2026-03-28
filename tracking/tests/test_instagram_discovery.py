from __future__ import annotations

import json
from io import StringIO

from django.core.management import call_command

from tracking.adapters.base import SeedResolution
from tracking.models import Platform
from tracking.services import platform_onboarding
from tracking.services.instagram_intent import build_instagram_discovery_intent, build_instagram_queries
from tracking.services.setup_runtime import SetupRunContext


def _seed(
    *,
    platform: str = Platform.INSTAGRAM,
    external_id: str = "seed-1",
    handle: str = "seedhandle",
    title: str = "Seed Title",
    description: str = "Seed description",
    url: str | None = None,
) -> SeedResolution:
    return SeedResolution(
        platform=platform,
        external_id=external_id,
        handle=handle,
        url=url or f"https://example.com/{handle}",
        title=title,
        description=description,
        uploads_playlist_id=None,
    )


def test_build_instagram_queries_for_creator_seed_prioritizes_creator_terms():
    seed = _seed(
        handle="doctormike",
        title="Doctor Mike",
        description="Doctor reacts to medical myths and explains health science",
    )
    intent = build_instagram_discovery_intent(
        seed=seed,
        linked_accounts=[],
        fallback_keywords=["family medicine residency", "medical myths", "doctor reacts"],
        recent_texts=["Doctor reacts to viral health misinformation", "Medical myths explained by a real doctor"],
    )

    queries = build_instagram_queries(intent)

    assert intent.entity_type.startswith("creator_")
    assert queries
    assert intent.query_classes[queries[0]] in {"creator", "format", "topic"}
    assert not queries[0].lower().startswith("family medicine residency")


def test_search_instagram_candidates_raw_skips_search_when_graph_pool_is_sufficient(monkeypatch):
    seed = _seed(handle="creatorlab", title="Creator Lab", description="tech creator systems")
    graph_candidates = [
        platform_onboarding._DiscoveryCandidate(
            platform=Platform.INSTAGRAM,
            external_id=f"ig-{idx}",
            handle=f"creator_{idx}",
            url=f"https://www.instagram.com/creator_{idx}/",
            display_name=f"Creator {idx}",
            description="tech creator reviews",
            query_hits={"seed_related"},
            metadata={"retrieval_source": "seed_related", "graph_depth": 1, "graph_hits": 1},
        )
        for idx in range(10)
    ]

    monkeypatch.setattr(platform_onboarding, "_seed_instagram_related_candidates", lambda **kwargs: list(graph_candidates))
    monkeypatch.setattr(platform_onboarding, "_build_cross_platform_instagram_candidates", lambda **kwargs: [])
    monkeypatch.setattr(platform_onboarding, "_expand_instagram_graph_frontier", lambda **kwargs: [])
    calls: list[str] = []

    def fake_cached_instagram_search_results(*, query, limit, context=None, purpose=None, context_id=None):
        calls.append(query)
        return []

    monkeypatch.setattr(platform_onboarding, "_cached_instagram_search_results", fake_cached_instagram_search_results)

    candidates = platform_onboarding._search_instagram_candidates_raw(
        keywords=["tech review", "creator"],
        competitors=[],
        seed_accounts=[seed],
        max_candidates=20,
    )

    assert len(candidates) == 10
    assert calls == []


def test_collector_aware_instagram_records_diagnostics_and_filters_institutions(monkeypatch):
    context = SetupRunContext()
    creator = platform_onboarding._DiscoveryCandidate(
        platform=Platform.INSTAGRAM,
        external_id="ig-good",
        handle="doctorcreator",
        url="https://www.instagram.com/doctorcreator/",
        display_name="Doctor Creator",
        description="Doctor reacts and explains health myths",
        query_hits={"doctor reacts"},
        metadata={"retrieval_source": "seed_related", "graph_support_count": 2, "graph_distance": 1},
    )
    institution = platform_onboarding._DiscoveryCandidate(
        platform=Platform.INSTAGRAM,
        external_id="ig-bad",
        handle="familymedicine.residency",
        url="https://www.instagram.com/familymedicine.residency/",
        display_name="Family Medicine Residency",
        description="Official residency program",
        query_hits={"family medicine residency"},
        metadata={"retrieval_source": "search", "graph_support_count": 0, "graph_distance": 3},
    )
    intent = build_instagram_discovery_intent(
        seed=_seed(
            handle="doctormike",
            title="Doctor Mike",
            description="Doctor reacts and explains health science",
        ),
        linked_accounts=[],
        fallback_keywords=["doctor reacts", "medical myths"],
        recent_texts=["Doctor reacts to medical myths", "Health science explained"],
    )
    creator.metadata["_instagram_intent"] = intent
    institution.metadata["_instagram_intent"] = intent
    monkeypatch.setattr(platform_onboarding, "_hydrate_instagram_candidates", lambda **kwargs: {})
    monkeypatch.setattr(
        platform_onboarding,
        "_batch_fetch_recent_instagram_reel_texts",
        lambda **kwargs: {
            "ig-good": (
                ["Doctor reacts to health myths", "Medical science explained simply"],
                [10000, 12000],
                4,
            ),
            "ig-bad": (
                ["Residency orientation", "Official residency schedule"],
                [500, 400],
                4,
            ),
        },
    )

    validated, reason = platform_onboarding._collector_aware_candidates(
        platform=Platform.INSTAGRAM,
        candidates=[creator, institution],
        keywords=["doctor reacts", "medical myths"],
        max_candidates=20,
        context=context,
    )

    assert reason == ""
    assert [candidate.external_id for candidate in validated] == ["ig-good"]
    diagnostics = context.discovery_diagnostics[Platform.INSTAGRAM]
    assert diagnostics["post_entity_gate_count"] == 1
    assert diagnostics["final_count"] == 1
    assert diagnostics["drop_reasons"]["entity_gate"] >= 1


def test_evaluate_instagram_discovery_command_outputs_json(monkeypatch):
    seed = _seed(handle="creatorlab", title="Creator Lab", description="Tech creator reviews")
    monkeypatch.setattr(
        "tracking.management.commands.evaluate_instagram_discovery.attempt_exact_seed_resolution",
        lambda raw_input, context=None: [type("Attempt", (), {"seed": seed, "error": None, "platform": Platform.INSTAGRAM})()],
    )
    monkeypatch.setattr(
        "tracking.management.commands.evaluate_instagram_discovery.infer_niche_keywords",
        lambda **kwargs: (["tech review", "creator"], "auto"),
    )
    candidate = platform_onboarding._DiscoveryCandidate(
        platform=Platform.INSTAGRAM,
        external_id="ig-1",
        handle="creator_one",
        url="https://www.instagram.com/creator_one/",
        display_name="Creator One",
        description="Tech creator",
        query_hits={"tech review"},
        metadata={"retrieval_source": "seed_related", "instagram_rank_total": 12.5},
    )
    monkeypatch.setattr(
        "tracking.management.commands.evaluate_instagram_discovery._search_instagram_candidates_raw",
        lambda **kwargs: [candidate],
    )
    monkeypatch.setattr(
        "tracking.management.commands.evaluate_instagram_discovery._collector_aware_candidates",
        lambda **kwargs: ([candidate], ""),
    )
    monkeypatch.setattr(
        "tracking.management.commands.evaluate_instagram_discovery._instagram_candidate_intent",
        lambda **kwargs: build_instagram_discovery_intent(
            seed=seed,
            linked_accounts=[],
            fallback_keywords=["tech review", "creator"],
            recent_texts=["Tech creator reviews"],
        ),
    )

    stdout = StringIO()
    call_command("evaluate_instagram_discovery", "--seed", "creatorlab", "--json", stdout=stdout)
    payload = json.loads(stdout.getvalue())

    assert payload["resolved_seed"]["handle"] == "creatorlab"
    assert payload["keywords"] == ["tech review", "creator"]
    assert payload["validated_count"] == 1
    assert payload["top_candidates"][0]["handle"] == "creator_one"
