from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from django.conf import settings
from django.utils import timezone

from botapp.telegram_api import send_message
from tracking.models import Competitor, ContentItem, Platform, Report, ReportStatus, TgUser, UserCompetitor
from tracking.services.collector import refresh_competitor
from tracking.services.platform_onboarding import (
    _find_instagram_profile_for_lookup,
    fetch_instagram_profiles_cached,
    fetch_tiktok_profile_feeds_cached,
)
from tracking.services.provider_config import get_tiktok_apify_config
from tracking.services.provider_runtime import ProviderFetchCache
from tracking.services.reporting import (
    build_report_payload,
    build_setup_verification_payload,
    render_report_text,
    split_telegram_text,
)
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


@dataclass(frozen=True)
class CollectionPassResult:
    competitors: list[Competitor]
    updated_items: list[ContentItem]
    successful_competitors: list[Competitor]
    collection_failures: list[ReportCollectionFailure]


def _send_report_messages(*, chat_id: int, text: str) -> dict[str, Any]:
    message_results = [send_message(chat_id=chat_id, text=chunk) for chunk in split_telegram_text(text=text)]
    return {
        "message_id": message_results[0]["message_id"],
        "message_ids": [result["message_id"] for result in message_results],
    }


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


def _chunked(values: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [values]
    return [values[index : index + size] for index in range(0, len(values), size)]


def _prime_provider_fetch_cache_for_competitors(
    *,
    competitors: list[Competitor],
    provider_fetch_cache: ProviderFetchCache,
) -> None:
    instagram_lookups = [
        str(competitor.handle or competitor.url or competitor.external_id or "").strip()
        for competitor in competitors
        if competitor.platform == Platform.INSTAGRAM
    ]
    tiktok_handles = [
        str(competitor.handle or (competitor.meta or {}).get("profile_handle") or competitor.external_id or "").strip().lstrip("@")
        for competitor in competitors
        if competitor.platform == Platform.TIKTOK
    ]

    for chunk in _chunked([lookup for lookup in instagram_lookups if lookup], 10):
        try:
            profiles = fetch_instagram_profiles_cached(
                inputs=chunk,
                purpose=provider_fetch_cache.purpose,
                context_id=provider_fetch_cache.context_id,
            )
        except Exception:
            logger.exception("Instagram batch prefetch failed for report collection")
            continue
        for lookup in chunk:
            profile = _find_instagram_profile_for_lookup(profiles, lookup)
            if profile:
                provider_fetch_cache.store_instagram_profile(profile=profile, lookups=[lookup])

    if tiktok_handles:
        config = get_tiktok_apify_config()
        max_results = min(
            int(getattr(settings, "YT_RECENT_N_FOR_METRICS", 15)),
            int(getattr(config, "results_per_profile", 10) or 10),
        )
        for chunk in _chunked([handle for handle in tiktok_handles if handle], 3):
            try:
                grouped = fetch_tiktok_profile_feeds_cached(
                    handles=chunk,
                    results_per_page=max(1, max_results),
                    purpose=provider_fetch_cache.purpose,
                    context_id=provider_fetch_cache.context_id,
                )
            except Exception:
                logger.exception("TikTok batch prefetch failed for report collection")
                continue
            for handle, items in grouped.items():
                if items:
                    provider_fetch_cache.store_tiktok_feed(handle=handle, items=items)


def _run_collection_pass(
    *,
    user: TgUser,
    period_end,
    provider_fetch_cache: ProviderFetchCache | None = None,
) -> CollectionPassResult:
    provider_fetch_cache = provider_fetch_cache or ProviderFetchCache(
        context_id=f"report:{user.id}:{int(period_end.timestamp())}",
        purpose="report_collection",
    )
    competitors = get_active_competitors(user=user)
    _prime_provider_fetch_cache_for_competitors(
        competitors=competitors,
        provider_fetch_cache=provider_fetch_cache,
    )
    updated_items: list[ContentItem] = []
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

    return CollectionPassResult(
        competitors=competitors,
        updated_items=updated_items,
        successful_competitors=successful_competitors,
        collection_failures=collection_failures,
    )


def build_report_preview(
    *,
    user: TgUser,
    period_start,
    period_end,
    provider_fetch_cache: ProviderFetchCache | None = None,
) -> ReportPreview:
    collection = _run_collection_pass(
        user=user,
        period_end=period_end,
        provider_fetch_cache=provider_fetch_cache,
    )

    if not collection.successful_competitors and collection.collection_failures:
        first_failure = collection.collection_failures[0]
        label = first_failure.competitor_display_name or first_failure.competitor_handle or str(first_failure.competitor_id)
        raise ReportPipelineError(
            f"all competitor refreshes failed; first failure: {first_failure.platform}:{label}: {first_failure.reason}"
        )

    competitor_by_item_id = {item.id: item.competitor for item in collection.updated_items}
    baseline_by_competitor_id = {
        competitor.id: compute_competitor_baseline(competitor=competitor, now=period_end)
        for competitor in collection.successful_competitors
    }
    scored = score_items_for_period(
        items=collection.updated_items,
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
            for failure in collection.collection_failures
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
        collection_failures=collection.collection_failures,
    )


def build_setup_verification_preview(
    *,
    user: TgUser,
    period_end,
    provider_fetch_cache: ProviderFetchCache | None = None,
) -> ReportPreview:
    collection = _run_collection_pass(
        user=user,
        period_end=period_end,
        provider_fetch_cache=provider_fetch_cache,
    )
    selected_counts = {platform: 0 for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM)}
    successful_counts = {platform: 0 for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM)}
    failures_by_platform: dict[str, list[ReportCollectionFailure]] = {
        platform: [] for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM)
    }
    examples_by_platform: dict[str, list[dict[str, Any]]] = {
        platform: [] for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM)
    }

    for competitor in collection.competitors:
        selected_counts[str(competitor.platform or "")] = selected_counts.get(str(competitor.platform or ""), 0) + 1
    for competitor in collection.successful_competitors:
        successful_counts[str(competitor.platform or "")] = successful_counts.get(str(competitor.platform or ""), 0) + 1
    for failure in collection.collection_failures:
        failures_by_platform.setdefault(failure.platform, []).append(failure)
    sorted_items = sorted(
        collection.updated_items,
        key=lambda item: (str(item.platform or ""), item.published_at, item.id),
        reverse=True,
    )
    for item in sorted_items:
        platform = str(item.platform or "")
        if len(examples_by_platform.setdefault(platform, [])) >= 3:
            continue
        competitor = item.competitor
        examples_by_platform[platform].append(
            {
                "title": str(item.title or "").strip() or str(item.external_id or ""),
                "url": str(item.url or "").strip(),
                "published_at": item.published_at.isoformat(),
                "competitor": str(competitor.display_name or competitor.handle or competitor.external_id or "").strip(),
                "content_type": str((getattr(item, "meta", None) or {}).get("content_type") or "").strip(),
            }
        )

    sections: list[dict[str, Any]] = []
    for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM):
        sections.append(
            {
                "platform": platform,
                "selected_competitors": selected_counts.get(platform, 0),
                "successful_competitors": successful_counts.get(platform, 0),
                "failed_competitors": len(failures_by_platform.get(platform, [])),
                "examples": examples_by_platform.get(platform, []),
                "failures": [
                    {
                        "competitor": failure.competitor_display_name or failure.competitor_handle or str(failure.competitor_id),
                        "reason": failure.reason,
                    }
                    for failure in failures_by_platform.get(platform, [])[:3]
                ],
            }
        )

    payload = build_setup_verification_payload(generated_at=period_end, sections=sections)
    text = render_report_text(payload=payload, timezone_str=user.timezone_str)
    section_counts = {
        str(section.get("platform")): len(section.get("examples") or []) for section in sections
    }
    return ReportPreview(
        payload=payload,
        text=text,
        section_counts=section_counts,
        collection_failures=collection.collection_failures,
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
    telegram_result = _send_report_messages(chat_id=int(user.tg_chat_id), text=preview.text)

    report.status = ReportStatus.SENT
    report.sent_at = timezone.now()
    report.save(update_fields=["status", "sent_at"])
    return SentReportResult(report=report, preview=preview, telegram_result=telegram_result)


def create_and_send_setup_verification_report(
    *,
    user: TgUser,
    period_start,
    period_end,
    provider_fetch_cache: ProviderFetchCache | None = None,
) -> SentReportResult:
    preview = build_setup_verification_preview(
        user=user,
        period_end=period_end,
        provider_fetch_cache=provider_fetch_cache,
    )
    report = Report.objects.create(
        user=user,
        period_start=period_start,
        period_end=period_end,
        status=ReportStatus.CREATED,
        payload=preview.payload,
    )
    telegram_result = _send_report_messages(chat_id=int(user.tg_chat_id), text=preview.text)

    report.status = ReportStatus.SENT
    report.sent_at = timezone.now()
    report.save(update_fields=["status", "sent_at"])
    return SentReportResult(report=report, preview=preview, telegram_result=telegram_result)
