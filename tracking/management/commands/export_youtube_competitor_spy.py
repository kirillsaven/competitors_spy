from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tracking.services.youtube_topic_video_collection import collect_youtube_topic_video_candidates


@dataclass(frozen=True)
class YouTubeSpyItem:
    rank: int
    channel_title: str
    channel_id: str
    url: str
    title: str
    published_at: str
    duration_seconds: int | None
    views: int
    likes: int | None
    comments: int | None
    score: float
    matched_queries: list[str]
    mechanism_guess: str
    why_it_may_have_worked: str
    adaptation_for_daria: str


def _read_queries(*, values: list[str], input_file: str | None) -> list[str]:
    queries = [str(value or "").strip() for value in values if str(value or "").strip()]
    if input_file:
        path = Path(input_file).expanduser()
        if not path.exists():
            raise CommandError(f"Input file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            clean = line.strip()
            if clean and not clean.startswith("#"):
                queries.append(clean)
    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(query)
    return deduped


def _preview(value: str, *, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def _mechanism_for_title(title: str, description: str) -> tuple[str, str]:
    text = f"{title}\n{description}".lower()
    if any(token in text for token in ("mistake", "ошиб", "don't", "не говори", "нельзя")):
        return "mistake / correction", "the title makes viewers check whether they are saying something incorrectly"
    if any(token in text for token in ("c1", "advanced", "продвинут", "native", "естествен")):
        return "advanced-status gap", "the video sells a higher-status version of familiar language"
    if any(token in text for token in ("phrase", "phrases", "фраз", "expressions", "vocabulary")):
        return "saveable phrase list", "the video is easy to save and reuse"
    if "?" in title:
        return "question hook", "the title invites a quick self-check before the answer"
    return "practical micro-lesson", "the video compresses one teachable language point into a short format"


def _to_item(candidate: Any, *, rank: int) -> YouTubeSpyItem:
    mechanism, why = _mechanism_for_title(candidate.title, candidate.description)
    title = _preview(candidate.title, limit=160)
    return YouTubeSpyItem(
        rank=rank,
        channel_title=str(candidate.channel_title or ""),
        channel_id=str(candidate.channel_id or ""),
        url=str(candidate.url or ""),
        title=title,
        published_at=candidate.published_at.isoformat(),
        duration_seconds=candidate.duration_seconds,
        views=int(candidate.views or 0),
        likes=candidate.likes,
        comments=candidate.comments,
        score=float(candidate.supplemental_score),
        matched_queries=list(candidate.matched_queries),
        mechanism_guess=mechanism,
        why_it_may_have_worked=why,
        adaptation_for_daria=(
            "Use the YouTube Shorts mechanic for Daria's Instagram: start with one visible language choice, "
            "show why a tutor's first version sounds too safe, then give the better C1 variant."
        ),
    )


def _render_markdown(*, items: list[YouTubeSpyItem], queries: list[str], diagnostics: dict[str, Any]) -> str:
    lines = [
        "# YouTube Competitor Spy Export",
        "",
        "## Queries",
        "",
        *[f"- `{query}`" for query in queries],
        "",
        "## Diagnostics",
        "",
        "```json",
        json.dumps(diagnostics, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Ranked Shorts",
        "",
        "| rank | channel | views | score | title | mechanism | why it may have worked | adaptation | url |",
        "| --- | --- | ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    for item in items:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item.rank),
                    item.channel_title.replace("|", "\\|"),
                    str(item.views),
                    f"{item.score:.2f}",
                    item.title.replace("|", "\\|"),
                    item.mechanism_guess.replace("|", "\\|"),
                    item.why_it_may_have_worked.replace("|", "\\|"),
                    item.adaptation_for_daria.replace("|", "\\|"),
                    f"[open]({item.url})" if item.url else "",
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


class Command(BaseCommand):
    help = "Export YouTube Shorts competitor-spy references for content ideation."

    def add_arguments(self, parser):
        parser.add_argument("--query", action="append", default=[], help="YouTube topic query. Repeatable.")
        parser.add_argument("--input-file", help="Text file with one query per line.")
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--min-output-items", type=int, default=1)
        parser.add_argument("--diagnostics-output", help="Optional JSON diagnostics output path.")
        parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
        parser.add_argument("--output", help="Optional output file. Defaults to stdout.")

    def handle(self, *args, **options):
        queries = _read_queries(values=list(options["query"] or []), input_file=options.get("input_file"))
        if not queries:
            raise CommandError("Provide at least one --query or --input-file")

        result = collect_youtube_topic_video_candidates(
            niche_keywords=queries,
            now=timezone.now(),
        )
        limit = max(1, int(options["limit"]))
        items = [_to_item(candidate, rank=index) for index, candidate in enumerate(result.candidates[:limit], start=1)]
        diagnostics: dict[str, Any] = {
            **result.diagnostics,
            "requested_queries": queries,
            "output_items": len(items),
            "status": "PASS",
            "failures": [],
        }
        if len(items) < max(0, int(options["min_output_items"])):
            diagnostics["status"] = "FAIL"
            diagnostics["failures"].append(f"output_items<{options['min_output_items']}")

        diagnostics_output = options.get("diagnostics_output")
        if diagnostics_output:
            diagnostics_path = Path(str(diagnostics_output)).expanduser()
            diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
            diagnostics_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")

        if diagnostics["status"] != "PASS":
            raise CommandError("; ".join(diagnostics["failures"]))

        if options["format"] == "json":
            output_text = json.dumps(
                {"queries": queries, "items": [asdict(item) for item in items], "diagnostics": diagnostics},
                ensure_ascii=False,
                indent=2,
            )
        else:
            output_text = _render_markdown(items=items, queries=queries, diagnostics=diagnostics)

        output_path = options.get("output")
        if output_path:
            path = Path(str(output_path)).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(output_text, encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Wrote YouTube competitor spy export: {path}"))
            return
        self.stdout.write(output_text)
