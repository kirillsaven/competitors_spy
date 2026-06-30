from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError


_NUMERIC_RE = re.compile(r"[\d\s.,]+")


@dataclass(frozen=True)
class BrowserSurfItem:
    rank: int
    account: str
    surface: str
    url: str
    published_at: str
    visible_views: int
    views_available: bool
    visible_likes: int
    visible_comments: int
    interactions: int
    ranking_source: str
    format: str
    hook_mechanism: str
    emotional_trigger: str
    viral_mechanic: str
    adaptation_for_daria: str
    what_to_copy: str
    what_not_to_copy: str
    confidence: str


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise CommandError(f"JSONL input not found: {path}")
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        clean = line.strip()
        if not clean:
            continue
        try:
            payload = json.loads(clean)
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid JSONL in {path}:{line_no}: {exc}") from exc
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _parse_visible_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return max(value, 0)
    raw = str(value or "").strip().lower()
    if not raw or raw in {"unavailable", "unknown", "n/a", "none"}:
        return 0
    multiplier = 1
    if "тыс" in raw or "k" in raw:
        multiplier = 1_000
    elif "млн" in raw or "m" in raw:
        multiplier = 1_000_000
    match = _NUMERIC_RE.search(raw)
    if not match:
        return 0
    number_text = match.group(0).strip().replace(" ", "")
    if "," in number_text and "." not in number_text and multiplier > 1:
        number_text = number_text.replace(",", ".")
    else:
        number_text = number_text.replace(",", "")
    try:
        return max(int(float(number_text) * multiplier), 0)
    except ValueError:
        return 0


def _text(value: Any, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def _item_from_row(row: dict[str, Any]) -> BrowserSurfItem:
    views = _parse_visible_int(row.get("visible_views"))
    likes = _parse_visible_int(row.get("visible_likes"))
    comments = _parse_visible_int(row.get("visible_comments"))
    interactions = likes + comments
    views_available = "visible_views" in row and views > 0
    ranking_source = "views" if views_available else "interactions"
    return BrowserSurfItem(
        rank=0,
        account=str(row.get("account") or row.get("handle") or "").strip().lstrip("@") or "unknown",
        surface=str(row.get("surface") or "reel").strip() or "reel",
        url=str(row.get("url") or "").strip(),
        published_at=str(row.get("published_at_if_visible") or row.get("published_at") or "").strip(),
        visible_views=views,
        views_available=views_available,
        visible_likes=likes,
        visible_comments=comments,
        interactions=interactions,
        ranking_source=ranking_source,
        format=_text(row.get("format")),
        hook_mechanism=_text(row.get("hook_mechanism")),
        emotional_trigger=_text(row.get("emotional_trigger")),
        viral_mechanic=_text(row.get("viral_mechanic")),
        adaptation_for_daria=_text(row.get("adaptation_for_daria")),
        what_to_copy=_text(row.get("what_to_copy")),
        what_not_to_copy=_text(row.get("what_not_to_copy")),
        confidence=str(row.get("confidence") or "").strip(),
    )


def _rank_key(item: BrowserSurfItem) -> tuple[int, int]:
    if item.views_available:
        return (item.visible_views, item.interactions)
    return (item.interactions, 0)


def _render_markdown(*, items: list[BrowserSurfItem], diagnostics: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Instagram Browser Surf Competitor Spy Export",
        "",
        "Source: logged-in browser surf JSONL. This is the fallback path when Instagram API providers are blocked.",
        "",
        "## Diagnostics",
        "",
        "```json",
        json.dumps(diagnostics, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Ranked References",
        "",
        "| rank | account | surface | views | likes | comments | ranking | format | trigger | viral mechanic | adaptation | url |",
        "| --- | --- | --- | ---: | ---: | ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for item in items:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item.rank),
                    f"@{item.account}".replace("|", "\\|"),
                    item.surface.replace("|", "\\|"),
                    str(item.visible_views),
                    str(item.visible_likes),
                    str(item.visible_comments),
                    item.ranking_source,
                    item.format.replace("|", "\\|"),
                    item.emotional_trigger.replace("|", "\\|"),
                    item.viral_mechanic.replace("|", "\\|"),
                    item.adaptation_for_daria.replace("|", "\\|"),
                    f"[open]({item.url})" if item.url else "",
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


class Command(BaseCommand):
    help = "Rank manually captured logged-in Instagram browser-surf JSONL as competitor-spy evidence."

    def add_arguments(self, parser):
        parser.add_argument("--input-jsonl", action="append", default=[], help="Manual surf JSONL file. Repeatable.")
        parser.add_argument("--reels-jsonl", help="Manual surf reels.jsonl file.")
        parser.add_argument("--carousels-jsonl", help="Manual surf carousels.jsonl file.")
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--min-items", type=int, default=1)
        parser.add_argument("--min-unique-accounts", type=int, default=1)
        parser.add_argument("--max-share-from-one-account", type=float, default=1.0)
        parser.add_argument("--diagnostics-output", help="Optional JSON diagnostics output path.")
        parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
        parser.add_argument("--output", help="Optional output file. Defaults to stdout.")

    def handle(self, *args, **options):
        paths = [Path(value).expanduser() for value in list(options["input_jsonl"] or [])]
        if options.get("reels_jsonl"):
            paths.append(Path(str(options["reels_jsonl"])).expanduser())
        if options.get("carousels_jsonl"):
            paths.append(Path(str(options["carousels_jsonl"])).expanduser())
        if not paths:
            raise CommandError("Provide --input-jsonl, --reels-jsonl, or --carousels-jsonl")

        raw_rows: list[dict[str, Any]] = []
        for path in paths:
            raw_rows.extend(_read_jsonl(path))

        seen_urls: set[str] = set()
        items: list[BrowserSurfItem] = []
        for row in raw_rows:
            item = _item_from_row(row)
            if not item.url or item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            items.append(item)

        items.sort(key=_rank_key, reverse=True)
        limit = max(1, int(options["limit"]))
        ranked = [BrowserSurfItem(**{**asdict(item), "rank": index}) for index, item in enumerate(items[:limit], start=1)]
        unique_accounts = {item.account.lower() for item in ranked if item.account}
        account_counts: dict[str, int] = {}
        for item in ranked:
            key = item.account.lower()
            account_counts[key] = account_counts.get(key, 0) + 1
        max_share = max((count / len(ranked) for count in account_counts.values()), default=0.0)
        diagnostics: dict[str, Any] = {
            "source_files": [str(path) for path in paths],
            "raw_rows": len(raw_rows),
            "deduped_items": len(items),
            "output_items": len(ranked),
            "unique_accounts": len(unique_accounts),
            "account_counts": account_counts,
            "max_share_from_one_account": round(max_share, 4),
            "status": "PASS",
            "failures": [],
        }
        if len(ranked) < max(0, int(options["min_items"])):
            diagnostics["failures"].append(f"output_items<{options['min_items']}")
        if len(unique_accounts) < max(0, int(options["min_unique_accounts"])):
            diagnostics["failures"].append(f"unique_accounts<{options['min_unique_accounts']}")
        max_allowed_share = float(options["max_share_from_one_account"])
        if ranked and max_allowed_share < 1.0 and max_share > max_allowed_share:
            diagnostics["failures"].append(f"max_share_from_one_account>{max_allowed_share}")
        if diagnostics["failures"]:
            diagnostics["status"] = "FAIL"

        diagnostics_output = options.get("diagnostics_output")
        if diagnostics_output:
            diagnostics_path = Path(str(diagnostics_output)).expanduser()
            diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
            diagnostics_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")

        if diagnostics["status"] != "PASS":
            raise CommandError("; ".join(diagnostics["failures"]))

        if options["format"] == "json":
            output_text = json.dumps(
                {"items": [asdict(item) for item in ranked], "diagnostics": diagnostics},
                ensure_ascii=False,
                indent=2,
            )
        else:
            output_text = _render_markdown(items=ranked, diagnostics=diagnostics)

        output_path = options.get("output")
        if output_path:
            path = Path(str(output_path)).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(output_text, encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Wrote Instagram browser surf export: {path}"))
            return
        self.stdout.write(output_text)
