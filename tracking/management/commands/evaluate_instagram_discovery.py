from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from tracking.models import Platform
from tracking.services.niche_service import infer_niche_keywords
from tracking.services.platform_onboarding import (
    _collector_aware_candidates,
    _instagram_candidate_intent,
    _search_instagram_candidates_raw,
)
from tracking.services.seed_resolver import attempt_exact_seed_resolution
from tracking.services.setup_runtime import SetupRunContext


class Command(BaseCommand):
    help = "Resolve a seed, run Instagram discovery, and print diagnostics for ranking/debugging."

    def add_arguments(self, parser):
        parser.add_argument("--seed", required=True, help="Seed handle or URL to resolve.")
        parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
        parser.add_argument(
            "--max-candidates",
            type=int,
            default=20,
            help="Target number of validated Instagram competitors.",
        )

    def handle(self, *args, **options):
        raw_seed = str(options["seed"] or "").strip()
        if not raw_seed:
            raise CommandError("--seed is required")

        context = SetupRunContext()
        attempts = attempt_exact_seed_resolution(raw_seed, context=context)
        successes = [attempt.seed for attempt in attempts if attempt.seed is not None]
        if not successes:
            details = [attempt.error for attempt in attempts if attempt.error]
            raise CommandError(details[0] if details else f"Failed to resolve seed: {raw_seed}")

        primary_seed = next((seed for seed in successes if seed.platform == Platform.INSTAGRAM), successes[0])
        linked_accounts = [seed for seed in successes if seed != primary_seed]
        keywords, source = infer_niche_keywords(
            seed=primary_seed,
            competitors=[],
            linked_accounts=linked_accounts,
            context=context,
        )
        raw_candidates = _search_instagram_candidates_raw(
            keywords=keywords,
            competitors=[],
            seed_accounts=[primary_seed] + linked_accounts,
            max_candidates=int(options["max_candidates"] or 20),
            context=context,
        )
        validated_candidates, empty_reason = _collector_aware_candidates(
            platform=Platform.INSTAGRAM,
            candidates=raw_candidates,
            keywords=keywords,
            max_candidates=int(options["max_candidates"] or 20),
            context=context,
        )
        intent = _instagram_candidate_intent(candidates=raw_candidates, keywords=keywords)

        output = {
            "resolved_seed": {
                "platform": primary_seed.platform,
                "external_id": primary_seed.external_id,
                "handle": primary_seed.handle,
                "title": primary_seed.title,
                "url": primary_seed.url,
            },
            "linked_accounts": [
                {
                    "platform": seed.platform,
                    "external_id": seed.external_id,
                    "handle": seed.handle,
                    "title": seed.title,
                    "url": seed.url,
                }
                for seed in linked_accounts
            ],
            "keywords": keywords,
            "keyword_source": source,
            "intent": intent.as_dict(),
            "raw_count": len(raw_candidates),
            "validated_count": len(validated_candidates),
            "empty_reason": empty_reason,
            "diagnostics": context.discovery_diagnostics.get(Platform.INSTAGRAM, {}),
            "top_candidates": [
                {
                    "external_id": candidate.external_id,
                    "handle": candidate.handle,
                    "display_name": candidate.display_name,
                    "url": candidate.url,
                    "query_hits": sorted(candidate.query_hits),
                    "score_total": candidate.metadata.get("instagram_rank_total"),
                    "entity_type_guess": candidate.metadata.get("entity_type_guess"),
                    "archetype_guess": candidate.metadata.get("archetype_guess"),
                    "retrieval_source": candidate.metadata.get("retrieval_source") or candidate.metadata.get("source"),
                }
                for candidate in validated_candidates[:20]
            ],
        }

        if options["json"]:
            self.stdout.write(json.dumps(output, ensure_ascii=False, indent=2))
            return

        self.stdout.write(f"Seed: {primary_seed.platform} {primary_seed.handle or primary_seed.external_id}")
        self.stdout.write(f"Keywords ({source}): {', '.join(keywords)}")
        self.stdout.write(f"Validated: {len(validated_candidates)} / {len(raw_candidates)}")
        if empty_reason:
            self.stdout.write(f"Reason: {empty_reason}")
        self.stdout.write(json.dumps(context.discovery_diagnostics.get(Platform.INSTAGRAM, {}), ensure_ascii=False, indent=2))
