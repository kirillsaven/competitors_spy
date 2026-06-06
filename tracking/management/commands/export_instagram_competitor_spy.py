from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from tracking.adapters.instagram import ApifyInstagramClient, InstagramApiError, extract_handle, profile_to_video_details
from tracking.services.provider_config import get_instagram_apify_config


@dataclass(frozen=True)
class InstagramSpyItem:
    rank: int
    competitor: str
    url: str
    published_at: str
    views: int
    likes: int | None
    comments: int | None
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


def _render_markdown(*, items: list[InstagramSpyItem], inputs: list[str]) -> str:
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
            "| rank | competitor | views | ER | mechanism | why it may have worked | adaptation | url |",
            "| --- | --- | ---: | ---: | --- | --- | --- | --- |",
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
                    str(item.views),
                    er,
                    item.mechanism_guess.replace("|", "\\|"),
                    item.why_it_may_have_worked.replace("|", "\\|"),
                    item.adaptation_prompt.replace("|", "\\|"),
                    f"[open]({item.url})" if item.url else "",
                ]
            )
            + " |"
        )
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


class Command(BaseCommand):
    help = "Export top recent Instagram Reels from competitor profiles as JSON or producer-ready markdown."

    def add_arguments(self, parser):
        parser.add_argument("--instagram", action="append", default=[], help="Instagram profile URL or handle. Repeatable.")
        parser.add_argument("--input-file", help="Text file with one Instagram handle/URL per line.")
        parser.add_argument("--limit", type=int, default=20, help="Maximum total Reels to output.")
        parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
        parser.add_argument("--output", help="Optional output file. Defaults to stdout.")

    def handle(self, *args, **options):
        inputs = _read_inputs(values=list(options["instagram"] or []), input_file=options.get("input_file"))
        if not inputs:
            raise CommandError("Provide at least one --instagram value or --input-file")

        config = get_instagram_apify_config()
        if config.provider != "apify":
            raise CommandError(f"Unsupported Instagram provider: {config.provider}. Set INSTAGRAM_PROVIDER=apify")
        if not config.access_token:
            raise CommandError("INSTAGRAM_PROVIDER_ACCESS_TOKEN is not set")

        client = ApifyInstagramClient(
            access_token=config.access_token,
            actor_id=config.profile_actor_id,
            search_actor_id=config.search_actor_id,
            base_url=config.base_url,
        )
        try:
            profiles = client.fetch_profiles(inputs=inputs)
        except InstagramApiError as exc:
            raise CommandError(str(exc)) from exc
        finally:
            client.close()

        rows: list[InstagramSpyItem] = []
        for profile in profiles:
            handle = str(profile.get("username") or profile.get("handle") or "").strip()
            competitor = f"@{handle}" if handle else str(profile.get("url") or "unknown")
            for detail in profile_to_video_details(profile):
                title = _text_preview(detail.title or detail.description)
                mechanism, why = _mechanism_guess(f"{detail.title}\n{detail.description}")
                rows.append(
                    InstagramSpyItem(
                        rank=0,
                        competitor=competitor,
                        url=detail.url,
                        published_at=detail.published_at.isoformat(),
                        views=detail.views,
                        likes=detail.likes,
                        comments=detail.comments,
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

        rows.sort(key=lambda item: (item.views, item.engagement_rate or 0.0), reverse=True)
        limit = max(1, int(options["limit"]))
        ranked = [
            InstagramSpyItem(**{**asdict(item), "rank": index})
            for index, item in enumerate(rows[:limit], start=1)
        ]

        if options["format"] == "json":
            payload: dict[str, Any] = {
                "inputs": inputs,
                "items": [asdict(item) for item in ranked],
            }
            output_text = json.dumps(payload, ensure_ascii=False, indent=2)
        else:
            output_text = _render_markdown(items=ranked, inputs=inputs)

        output_path = options.get("output")
        if output_path:
            path = Path(str(output_path)).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(output_text, encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Wrote Instagram competitor spy export: {path}"))
            return

        self.stdout.write(output_text)
