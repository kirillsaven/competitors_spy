from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from django.conf import settings
from django.utils import timezone

from botapp.telegram_api import send_message
from tracking.models import Competitor, Platform, Report, ReportStatus, TgUser, UserCompetitor
from tracking.services.collector import refresh_competitor
from tracking.services.provider_runtime import ProviderFetchCache
from tracking.services.reporting import build_report_payload, render_report_text
from tracking.services.scoring import compute_competitor_baseline, score_items_for_period

logger = logging.getLogger(__name__)


class ReportPipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReportCollectionFailure:
    platform: str
    competitor_id: int
    competitor_display_name: str
    competitor_handle: str
    reason: str


@dataclass(frozen=True)
class ReportPreview:
    payload: dict[str, Any]
    text: str
    section_counts: dict[str, int]
    collection_failures: list[ReportCollectionFailure]


@dataclass(frozen=True)
class SentReportResult:
    report: Report
    preview: ReportPreview
    telegram_result: dict[str, Any]


def get_active_competitors(*, user: TgUser) -> list[Competitor]:
    max_competitors = int(getattr(settings, "MAX_COMPETITORS_PER_PLATFORM", 20))
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


def _short_reason(exc: Exception) -> str:
    text = " ".join(str(exc or "").split()).strip()
    if not text:
        return exc.__class__.__name__
    if len(text) > 180:
        return text[:177] + "..."
    return text


def build_report_preview(
    *,
    user: TgUser,
    period_start,
    period_end,
    provider_fetch_cache: ProviderFetchCache | None = None,
) -> ReportPreview:
    competitors = get_active_competitors(user=user)

    updated_items = []
    successful_competitors: list[Competitor] = []
    collection_failures: list[ReportCollectionFailure] = []
    for competitor in competitors:
        try:
            competitor_items = refresh_competitor(
                competitor=competitor,
                mode="incremental",
                captured_at=period_end,
                provider_fetch_cache=provider_fetch_cache,
            )
        except Exception as exc:
            logger.exception(
                "Competitor refresh failed during report preview (user_id=%s, competitor_id=%s, platform=%s)",
                user.id,
                competitor.id,
                competitor.platform,
            )
            collection_failures.append(
                ReportCollectionFailure(
                    platform=str(competitor.platform or ""),
                    competitor_id=int(competitor.id),
                    competitor_display_name=str(competitor.display_name or ""),
                    competitor_handle=str(competitor.handle or ""),
                    reason=_short_reason(exc),
                )
            )
            continue
        successful_competitors.append(competitor)
        updated_items.extend(competitor_items)

    if not successful_competitors and collection_failures:
        first_failure = collection_failures[0]
        label = first_failure.competitor_display_name or first_failure.competitor_handle or str(first_failure.competitor_id)
        raise ReportPipelineError(
            f"all competitor refreshes failed; first failure: {first_failure.platform}:{label}: {first_failure.reason}"
        )

    competitor_by_item_id = {item.id: item.competitor for item in updated_items}
    baseline_by_competitor_id = {
        competitor.id: compute_competitor_baseline(competitor=competitor, now=period_end)
        for competitor in successful_competitors
    }
    scored = score_items_for_period(
        items=updated_items,
        competitor_by_item_id=competitor_by_item_id,
        baseline_by_competitor_id=baseline_by_competitor_id,
        period_start=period_start,
        period_end=period_end,
    )

    payload = build_report_payload(
        scored=scored,
        period_start=period_start,
        period_end=period_end,
        collection_failures=[
            {
                "platform": failure.platform,
                "competitor": {
                    "id": failure.competitor_id,
                    "display_name": failure.competitor_display_name,
                    "handle": failure.competitor_handle,
                },
                "reason": failure.reason,
            }
            for failure in collection_failures
        ],
    )
    text = render_report_text(payload=payload, timezone_str=user.timezone_str)
    section_counts = {
        str(section.get("platform")): len(section.get("items") or []) for section in (payload.get("sections") or [])
    }
    return ReportPreview(
        payload=payload,
        text=text,
        section_counts=section_counts,
        collection_failures=collection_failures,
    )


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
    provider_fetch_cache: ProviderFetchCache | None = None,
) -> SentReportResult:
    preview = build_report_preview(
        user=user,
        period_start=period_start,
        period_end=period_end,
        provider_fetch_cache=provider_fetch_cache,
    )
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
