from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from django.core.management import call_command


@dataclass(frozen=True)
class FakeCandidate:
    video_id: str
    url: str
    title: str
    description: str
    published_at: datetime
    duration_seconds: int
    views: int
    likes: int | None
    comments: int | None
    channel_id: str
    channel_title: str
    matched_queries: tuple[str, ...]
    hit_count: int
    first_seen_rank: int
    query_positions: dict[str, int]
    supplemental_score: float
    supplemental_ranking_factors: dict[str, float]
    supplemental_survival_reason: str


@dataclass(frozen=True)
class FakeResult:
    queries: tuple[str, ...]
    diagnostics: dict
    candidates: tuple[FakeCandidate, ...]


def test_export_youtube_competitor_spy_writes_ranked_json(tmp_path, monkeypatch):
    from tracking.management.commands import export_youtube_competitor_spy as command_module

    def fake_collect(*, niche_keywords, now):
        assert niche_keywords == ["C1 English teacher", "advanced english phrases"]
        return FakeResult(
            queries=tuple(niche_keywords),
            diagnostics={"final_candidates": 1},
            candidates=(
                FakeCandidate(
                    video_id="yt-1",
                    url="https://www.youtube.com/watch?v=yt-1",
                    title="C1 English phrases you still say too basically",
                    description="Advanced phrase examples",
                    published_at=datetime(2026, 6, 30, 10, 0, tzinfo=UTC),
                    duration_seconds=42,
                    views=15000,
                    likes=900,
                    comments=40,
                    channel_id="UC123",
                    channel_title="English Coach",
                    matched_queries=("C1 English teacher",),
                    hit_count=1,
                    first_seen_rank=1,
                    query_positions={"C1 English teacher": 1},
                    supplemental_score=0.82,
                    supplemental_ranking_factors={"niche_phrase_match": 0.28},
                    supplemental_survival_reason="deterministic_rank_pass",
                ),
            ),
        )

    monkeypatch.setattr(command_module, "collect_youtube_topic_video_candidates", fake_collect)
    output = tmp_path / "youtube.json"
    diagnostics = tmp_path / "youtube_diagnostics.json"

    call_command(
        "export_youtube_competitor_spy",
        "--query",
        "C1 English teacher",
        "--query",
        "advanced english phrases",
        "--diagnostics-output",
        str(diagnostics),
        "--format",
        "json",
        "--output",
        str(output),
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    diag = json.loads(diagnostics.read_text(encoding="utf-8"))

    assert payload["items"][0]["channel_title"] == "English Coach"
    assert payload["items"][0]["mechanism_guess"] == "advanced-status gap"
    assert payload["items"][0]["views"] == 15000
    assert diag["status"] == "PASS"
