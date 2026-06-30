from __future__ import annotations

import json

from django.core.management import call_command


def test_export_instagram_browser_surf_ranks_manual_jsonl(tmp_path):
    reels = tmp_path / "reels.jsonl"
    reels.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "url": "https://www.instagram.com/reel/a/",
                        "account": "teacher_one",
                        "surface": "reel",
                        "visible_likes": "120",
                        "visible_comments": "5",
                        "format": "self-check",
                        "hook_mechanism": "question",
                        "emotional_trigger": "teacher wants to know if her answer is too basic",
                        "viral_mechanic": "comments with answers",
                        "adaptation_for_daria": "C1 self-check",
                        "what_to_copy": "one phrase on screen",
                        "what_not_to_copy": "shaming",
                        "confidence": "high",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "url": "https://www.instagram.com/reel/b/",
                        "account": "teacher_two",
                        "surface": "reel",
                        "visible_views": "14.2K",
                        "visible_likes": "900",
                        "visible_comments": "40",
                        "format": "advanced phrase",
                        "hook_mechanism": "status gap",
                        "emotional_trigger": "advanced speaker wants sharper wording",
                        "viral_mechanic": "saveable rewrite",
                        "adaptation_for_daria": "basic vs C1 variant",
                        "what_to_copy": "fast before-after",
                        "what_not_to_copy": "native-speaker superiority",
                        "confidence": "high",
                    },
                    ensure_ascii=False,
                ),
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "browser_export.json"
    diagnostics = tmp_path / "diagnostics.json"

    call_command(
        "export_instagram_browser_surf",
        "--reels-jsonl",
        str(reels),
        "--min-items",
        "2",
        "--min-unique-accounts",
        "2",
        "--diagnostics-output",
        str(diagnostics),
        "--format",
        "json",
        "--output",
        str(output),
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    diag = json.loads(diagnostics.read_text(encoding="utf-8"))

    assert payload["items"][0]["url"] == "https://www.instagram.com/reel/b/"
    assert payload["items"][0]["visible_views"] == 14200
    assert payload["items"][1]["ranking_source"] == "interactions"
    assert diag["status"] == "PASS"
    assert diag["unique_accounts"] == 2
