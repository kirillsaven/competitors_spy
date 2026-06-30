from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from tracking.adapters.instagram import (
    ApifyInstagramClient,
    GwaaInstagramClient,
    InstagramApiError,
    extract_handle,
    profile_to_video_details,
)
from tracking.services.provider_config import get_instagram_apify_config


@dataclass(frozen=True)
class InstagramSpyItem:
    rank: int
    competitor: str
    content_type: str
    url: str
    published_at: str
    views: int
    views_available: bool
    likes: int | None
    comments: int | None
    interactions: int
    ranking_source: str
    engagement_rate: float | None
    title: str
    mechanism_guess: str
    why_it_may_have_worked: str
    adaptation_prompt: str


def _read_inputs(*, values: list[str], input_file: str | None) -> list[str]:
    out: list[str] = []
    out.extend(str(value or "").strip() for value in values if str(value or "").strip())
    if input_file:
        path = Path(input_file).expanduser()
        if not path.exists():
            raise CommandError(f"Input file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            clean = line.strip()
            if not clean or clean.startswith("#"):
                continue
            out.append(clean)
    deduped: list[str] = []
    seen: set[str] = set()
    for value in out:
        key = (extract_handle(value) or value).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def _text_preview(value: str, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _engagement_rate(*, views: int, likes: int | None, comments: int | None) -> float | None:
    if views <= 0:
        return None
    interactions = int(likes or 0) + int(comments or 0)
    if interactions <= 0:
        return None
    return round(interactions / views, 4)


def _rank_score(item: InstagramSpyItem) -> tuple[int, float]:
    if item.ranking_source == "views" and item.views > 0:
        return (item.views, item.engagement_rate or 0.0)
    return (item.interactions, 0.0)


def _mechanism_guess(text: str) -> tuple[str, str]:
    lowered = text.lower()
    if any(token in lowered for token in ("ошиб", "mistake", "wrong", "don't", "не делай", "нельзя")):
        return (
            "mistake / prohibition",
            "negative framing makes the viewer check whether they are doing the wrong thing",
        )
    if any(token in lowered for token in ("секрет", "secret", "никто", "truth", "правда")):
        return (
            "hidden truth",
            "the post promises access to something usually withheld or underexplained",
        )
    if any(token in lowered for token in ("до", "после", "before", "after", "vs", "versus")):
        return (
            "contrast / before-after",
            "the post creates a visible gap and makes the payoff easy to understand fast",
        )
    if any(token in lowered for token in ("как", "how to", "способ", "формула", "framework")):
        return (
            "practical how-to",
            "the post offers a concrete utility promise that is easy to save",
        )
    if any(token in lowered for token in ("история", "story", "я ", "мне ", "мой ", "моя ")):
        return (
            "personal story",
            "the post uses creator-specific proof and makes the lesson feel lived-in",
        )
    return (
        "identity / curiosity",
        "the post likely works through audience self-recognition plus a curiosity gap",
    )


def _adaptation_prompt(*, mechanism: str, title: str) -> str:
    base = _text_preview(title, limit=120) or "no visible caption"
    return (
        "Adapt the mechanism, not the wording, for Daria's English tutor audience: "
        f"use `{mechanism}` to create a Reel about teaching mode vs real speaking. "
        f"Reference source idea: `{base}`."
    )


def _render_markdown(
    *,
    items: list[InstagramSpyItem],
    inputs: list[str],
    provider_status: dict[str, dict[str, Any]] | None = None,
    discovery_status: dict[str, Any] | None = None,
) -> str:
    lines: list[str] = [
        "# Instagram Competitor Spy Export",
        "",
        "## Inputs",
        "",
    ]
    lines.extend(f"- `{value}`" for value in inputs)
    lines.extend(
        [
            "",
            "## Top Reels",
            "",
            "| rank | competitor | type | views | views available | interactions | ranking | ER | mechanism | why it may have worked | adaptation | url |",
            "| --- | --- | --- | ---: | --- | ---: | --- | ---: | --- | --- | --- | --- |",
        ]
    )
    for item in items:
        er = "" if item.engagement_rate is None else f"{item.engagement_rate:.2%}"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item.rank),
                    item.competitor.replace("|", "\\|"),
                    item.content_type,
                    str(item.views),
                    "yes" if item.views_available else "no",
                    str(item.interactions),
                    item.ranking_source,
                    er,
                    item.mechanism_guess.replace("|", "\\|"),
                    item.why_it_may_have_worked.replace("|", "\\|"),
                    item.adaptation_prompt.replace("|", "\\|"),
                    f"[open]({item.url})" if item.url else "",
                ]
            )
            + " |"
        )
    if provider_status:
        lines.extend(
            [
                "",
                "## Provider Status",
                "",
                "| input | status | error |",
                "| --- | --- | --- |",
            ]
        )
        for key, value in provider_status.items():
            error = _text_preview(str(value.get("error") or ""), limit=160).replace("|", "\\|")
            lines.append(
                f"| `{key}` | `{value.get('status', '')}` | {error} |"
            )
    if discovery_status:
        lines.extend(["", "## Discovery Status", "", "```json", json.dumps(discovery_status, ensure_ascii=False, indent=2), "```"])
    lines.extend(
        [
            "",
            "## Producer Notes",
            "",
            "- Use this as raw intelligence, not as scripts to copy.",
            "- Prefer mechanisms with clear first-frame tension, status pressure, proof, or a saveable teaching point.",
            "- Every adapted idea must be rewritten for Daria's niche: Russian-speaking English tutors and professional speaking practice.",
        ]
    )
    return "\n".join(lines) + "\n"


def _profile_status_key(raw: str) -> str:
    return extract_handle(raw) or str(raw or "").strip() or "unknown"


def _profile_from_profiles(raw: str, profiles: list[dict[str, Any]]) -> dict[str, Any] | None:
    expected = (extract_handle(raw) or "").lower()
    if not profiles:
        return None
    if not expected:
        return profiles[0]
    for profile in profiles:
        handle = str(profile.get("username") or profile.get("handle") or "").strip().lower()
        if handle == expected:
            return profile
    return profiles[0]


def _fetch_profiles_resilient(
    *,
    client: Any,
    inputs: list[str],
    continue_on_error: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    provider_status: dict[str, dict[str, Any]] = {}
    if not continue_on_error:
        profiles = client.fetch_profiles(inputs=inputs)
        for raw in inputs:
            status = "ok" if _profile_from_profiles(raw, profiles) else "empty"
            provider_status[_profile_status_key(raw)] = {"status": status, "error": None}
        return profiles, provider_status

    profiles: list[dict[str, Any]] = []
    for raw in inputs:
        key = _profile_status_key(raw)
        try:
            fetched = client.fetch_profiles(inputs=[raw])
        except InstagramApiError as exc:
            provider_status[key] = {"status": "failed", "error": str(exc)}
            continue
        if not fetched:
            provider_status[key] = {"status": "empty", "error": None}
            continue
        provider_status[key] = {"status": "ok", "error": None}
        profiles.extend(fetched)
    return profiles, provider_status


def _discover_inputs(
    *,
    client: Any,
    queries: list[str],
    seed_handles: list[str],
    max_discovered_profiles: int,
) -> tuple[list[str], dict[str, Any]]:
    discovered: list[str] = []
    status: dict[str, Any] = {
        "status": "NOT_REQUESTED",
        "queries": queries,
        "seed_handles": seed_handles,
        "discovered_count": 0,
        "errors": {},
    }
    if not queries and not seed_handles:
        return discovered, status
    if not hasattr(client, "search_profiles"):
        status["status"] = "DISCOVERY_BLOCKED"
        status["reason"] = "provider_does_not_support_profile_search"
        return discovered, status

    seen: set[str] = set()
    for query in [*queries, *seed_handles]:
        try:
            profiles = client.search_profiles(query=query, limit=max_discovered_profiles)
        except InstagramApiError as exc:
            status["errors"][query] = str(exc)
            continue
        for profile in profiles:
            handle = str(profile.get("username") or profile.get("handle") or "").strip()
            if not handle:
                continue
            key = handle.lower()
            if key in seen:
                continue
            seen.add(key)
            discovered.append(handle)
            if len(discovered) >= max_discovered_profiles:
                break
        if len(discovered) >= max_discovered_profiles:
            break
    status["discovered_count"] = len(discovered)
    status["status"] = "OK" if discovered else "DISCOVERY_BLOCKED"
    if not discovered and not status["errors"]:
        status["reason"] = "search_returned_no_profiles"
    return discovered, status


class Command(BaseCommand):
    help = "Export top recent Instagram Reels from competitor profiles as JSON or producer-ready markdown."

    def add_arguments(self, parser):
        parser.add_argument("--instagram", action="append", default=[], help="Instagram profile URL or handle. Repeatable.")
        parser.add_argument("--input-file", help="Text file with one Instagram handle/URL per line.")
        parser.add_argument("--limit", type=int, default=50, help="Maximum total items to output.")
        parser.add_argument("--per-profile-limit", type=int, default=20, help="Maximum items to keep per profile before global ranking.")
        parser.add_argument("--include-carousels", action="store_true", help="Include carousel/sidebar posts when the provider returns them.")
        parser.add_argument("--include-posts", action="store_true", help="Include feed/photo posts when the provider returns them.")
        parser.add_argument("--continue-on-error", action="store_true", help="Continue when a profile/provider request fails.")
        parser.add_argument("--min-successful-profiles", type=int, default=1, help="Fail if fewer profiles return usable data.")
        parser.add_argument("--min-output-items", type=int, default=1, help="Fail if fewer ranked items are produced.")
        parser.add_argument("--diagnostics-output", help="Optional JSON diagnostics output file.")
        parser.add_argument("--discovery-query", action="append", default=[], help="Profile discovery query. Repeatable.")
        parser.add_argument("--seed-handle", action="append", default=[], help="Seed handle for provider-side profile discovery. Repeatable.")
        parser.add_argument("--discover-similar", action="store_true", help="Attempt provider-side discovery from seed handles when supported.")
        parser.add_argument("--max-discovered-profiles", type=int, default=20, help="Maximum discovered profiles to add.")
        parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
        parser.add_argument("--output", help="Optional output file. Defaults to stdout.")

    def handle(self, *args, **options):
        inputs = _read_inputs(values=list(options["instagram"] or []), input_file=options.get("input_file"))
        if not inputs:
            raise CommandError("Provide at least one --instagram value or --input-file")

        config = get_instagram_apify_config()
        if config.provider == "apify":
            if not config.access_token:
                raise CommandError("INSTAGRAM_PROVIDER_ACCESS_TOKEN is not set")
            client = ApifyInstagramClient(
                access_token=config.access_token,
                actor_id=config.profile_actor_id,
                search_actor_id=config.search_actor_id,
                base_url=config.base_url,
            )
        elif config.provider in {"gwaa", "public", "noauth"}:
            client = GwaaInstagramClient(base_url=config.base_url or "https://highlights.gwaa.net")
        else:
            raise CommandError(
                f"Unsupported Instagram provider: {config.provider}. Set INSTAGRAM_PROVIDER=apify or gwaa"
            )
        discovery_queries = list(options.get("discovery_query") or [])
        seed_handles = list(options.get("seed_handle") or [])
        if options.get("discover_similar"):
            discovery_queries.extend(seed_handles)
        discovered_inputs, discovery_status = _discover_inputs(
            client=client,
            queries=discovery_queries,
            seed_handles=seed_handles,
            max_discovered_profiles=max(1, int(options["max_discovered_profiles"])),
        )
        inputs = _read_inputs(values=[*inputs, *discovered_inputs], input_file=None)
        try:
            profiles, provider_status = _fetch_profiles_resilient(
                client=client,
                inputs=inputs,
                continue_on_error=bool(options["continue_on_error"]),
            )
        except InstagramApiError as exc:
            raise CommandError(str(exc)) from exc
        finally:
            client.close()

        rows: list[InstagramSpyItem] = []
        for profile in profiles:
            handle = str(profile.get("username") or profile.get("handle") or "").strip()
            competitor = f"@{handle}" if handle else str(profile.get("url") or "unknown")
            profile_rows: list[InstagramSpyItem] = []
            for detail in profile_to_video_details(
                profile,
                include_carousels=bool(options["include_carousels"]),
                include_posts=bool(options["include_posts"]),
            ):
                title = _text_preview(detail.title or detail.description)
                mechanism, why = _mechanism_guess(f"{detail.title}\n{detail.description}")
                interactions = int(detail.likes or 0) + int(detail.comments or 0)
                profile_rows.append(
                    InstagramSpyItem(
                        rank=0,
                        competitor=competitor,
                        content_type=detail.content_type,
                        url=detail.url,
                        published_at=detail.published_at.isoformat(),
                        views=detail.views,
                        views_available=detail.views_available,
                        likes=detail.likes,
                        comments=detail.comments,
                        interactions=interactions,
                        ranking_source=detail.ranking_source,
                        engagement_rate=_engagement_rate(
                            views=detail.views,
                            likes=detail.likes,
                            comments=detail.comments,
                        ),
                        title=title,
                        mechanism_guess=mechanism,
                        why_it_may_have_worked=why,
                        adaptation_prompt=_adaptation_prompt(mechanism=mechanism, title=title),
                    )
                )
            profile_rows.sort(key=_rank_score, reverse=True)
            rows.extend(profile_rows[: max(1, int(options["per_profile_limit"]))])

        rows.sort(key=_rank_score, reverse=True)
        limit = max(1, int(options["limit"]))
        ranked = [
            InstagramSpyItem(**{**asdict(item), "rank": index})
            for index, item in enumerate(rows[:limit], start=1)
        ]

        successful_profiles = sum(1 for item in provider_status.values() if item.get("status") == "ok")
        diagnostics: dict[str, Any] = {
            "inputs": inputs,
            "provider_status": provider_status,
            "successful_profiles": successful_profiles,
            "output_items": len(ranked),
            "discovery": discovery_status,
            "include_carousels": bool(options["include_carousels"]),
            "include_posts": bool(options["include_posts"]),
        }
        if successful_profiles < max(0, int(options["min_successful_profiles"])):
            raise CommandError(
                f"Only {successful_profiles} successful profiles; required {options['min_successful_profiles']}"
            )
        if len(ranked) < max(0, int(options["min_output_items"])):
            raise CommandError(f"Only {len(ranked)} output items; required {options['min_output_items']}")

        diagnostics_output = options.get("diagnostics_output")
        if diagnostics_output:
            diagnostics_path = Path(str(diagnostics_output)).expanduser()
            diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
            diagnostics_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")

        if options["format"] == "json":
            payload: dict[str, Any] = {
                "inputs": inputs,
                "items": [asdict(item) for item in ranked],
                "diagnostics": diagnostics,
            }
            output_text = json.dumps(payload, ensure_ascii=False, indent=2)
        else:
            output_text = _render_markdown(
                items=ranked,
                inputs=inputs,
                provider_status=provider_status,
                discovery_status=discovery_status,
            )

        output_path = options.get("output")
        if output_path:
            path = Path(str(output_path)).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(output_text, encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Wrote Instagram competitor spy export: {path}"))
            return

        self.stdout.write(output_text)
