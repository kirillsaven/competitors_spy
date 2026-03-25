from __future__ import annotations

import io
import json
from types import SimpleNamespace

from django.core.management import call_command


def test_send_test_platform_report_prints_report_text_and_message_id(monkeypatch):
    from tracking.management.commands import send_test_platform_report
    from tracking.services.report_pipeline import ReportPreview, SentReportResult

    monkeypatch.setattr(
        send_test_platform_report,
        "prepare_live_platform_user",
        lambda **kwargs: SimpleNamespace(
            user=SimpleNamespace(id=1),
            resolved_rows=[{"platform": "tiktok", "input": "nba"}],
            required_platforms={"tiktok"},
        ),
    )
    monkeypatch.setattr(
        send_test_platform_report,
        "create_and_send_report",
        lambda **kwargs: SentReportResult(
            report=SimpleNamespace(id=42),
            preview=ReportPreview(
                payload={"sections": []},
                text="TikTok:\n1) Test item\n",
                section_counts={"tiktok": 1},
            ),
            telegram_result={"message_id": 777, "chat": {"id": 123}},
        ),
    )

    out = io.StringIO()
    call_command(
        "send_test_platform_report",
        "--tg-user-id",
        "123",
        "--tiktok",
        "nba",
        stdout=out,
    )

    rendered = out.getvalue()
    assert "TikTok:\n1) Test item" in rendered
    payload = json.loads(rendered.split("\n\n", 1)[1])
    assert payload["message_id"] == 777
    assert payload["report_id"] == 42
