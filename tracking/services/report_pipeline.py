from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.utils import timezone

from botapp.telegram_api import send_message
from tracking.models import Competitor, Platform, Report, ReportStatus, TgUser, UserCompetitor
from tracking.services.collector import refresh_competitor
from tracking.services.reporting import build_report_payload, render_report_text
from tracking.services.scoring import compute_competitor_baseline, score_items_for_period


class ReportPipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReportPreview:
    payload: dict[str, Any]
    text: str
    section_counts: dict[str, int]


@dataclass(frozen=True)
class SentReportResult:
    report: Report
    preview: ReportPreview
    telegram_result: dict[str, Any]


def get_active_competitors(*, user: TgUser) -> list[Competitor]:
    max_competitors = int(getattr(settings, "MAX_COMPETITORS_YOUTUBE", 20))
    competitors: list[Competitor] = []
    for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM):
        links = (
            UserCompetitor.objects.select_related("competitor")
            .filter(user=user, is_active=True, competitor__platform=platform)
            .order_by("id")
            .all()[:max_competitors]
        )
        competitors.extend(link.competitor for link in links)
    return competitors


def build_report_preview(*, user: TgUser, period_start, period_end) -> ReportPreview:
    competitors = get_active_competitors(user=user)

    updated_items = []
    for competitor in competitors:
        updated_items.extend(
            refresh_competitor(
                competitor=competitor,
                mode="incremental",
                captured_at=period_end,
            )
        )

    competitor_by_item_id = {item.id: item.competitor for item in updated_items}
    baseline_by_competitor_id = {
        competitor.id: compute_competitor_baseline(competitor=competitor, now=period_end) for competitor in competitors
    }
    scored = score_items_for_period(
        items=updated_items,
        competitor_by_item_id=competitor_by_item_id,
        baseline_by_competitor_id=baseline_by_competitor_id,
        period_start=period_start,
        period_end=period_end,
    )

    payload = build_report_payload(scored=scored, period_start=period_start, period_end=period_end)
    text = render_report_text(payload=payload, timezone_str=user.timezone_str)
    section_counts = {
        str(section.get("platform")): len(section.get("items") or []) for section in (payload.get("sections") or [])
    }
    return ReportPreview(payload=payload, text=text, section_counts=section_counts)


def assert_required_platform_sections(*, preview: ReportPreview, required_platforms: set[str]) -> None:
    for platform in required_platforms:
        if preview.section_counts.get(platform, 0) <= 0:
            raise ReportPipelineError(
                f"{platform} verification produced an empty report section with the current provider data and scoring thresholds"
            )


def create_and_send_report(
    *,
    user: TgUser,
    period_start,
    period_end,
    required_platforms: set[str] | None = None,
) -> SentReportResult:
    preview = build_report_preview(user=user, period_start=period_start, period_end=period_end)
    if required_platforms:
        assert_required_platform_sections(preview=preview, required_platforms=required_platforms)

    report = Report.objects.create(
        user=user,
        period_start=period_start,
        period_end=period_end,
        status=ReportStatus.CREATED,
        payload=preview.payload,
    )
    telegram_result = send_message(chat_id=int(user.tg_chat_id), text=preview.text)

    report.status = ReportStatus.SENT
    report.sent_at = timezone.now()
    report.save(update_fields=["status", "sent_at"])
    return SentReportResult(report=report, preview=preview, telegram_result=telegram_result)
