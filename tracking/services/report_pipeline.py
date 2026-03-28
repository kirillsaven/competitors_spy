from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from statistics import mean
from typing import Any

from django.conf import settings
from django.utils import timezone

from botapp.telegram_api import send_message
from tracking.models import (
    Competitor,
    ContentItem,
    MetricSnapshot,
    Platform,
    Report,
    ReportStatus,
    TgUser,
    UserCompetitor,
)
from tracking.services.collector import refresh_competitor
from tracking.services.platform_onboarding import (
    PlatformOnboardingError,
    _find_instagram_profile_for_lookup,
    fetch_instagram_profiles_cached,
    fetch_tiktok_profile_feeds_cached,
)
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
class ReportPreview:
    payload: dict[str, Any]
    text: str
    section_counts: dict[str, int]


@dataclass(frozen=True)
class SentReportResult:
    report: Report
    preview: ReportPreview
    telegram_result: dict[str, Any]


SETUP_REPORT_MAX_COMPETITORS_PER_PLATFORM = 20


def _ensure_provider_fetch_cache(
    *,
    provider_fetch_cache: ProviderFetchCache | None,
    purpose: str,
) -> ProviderFetchCache:
    if provider_fetch_cache is not None:
        if not str(provider_fetch_cache.purpose or "").strip():
            provider_fetch_cache.purpose = purpose
        return provider_fetch_cache
    return ProviderFetchCache(purpose=purpose)


def _collection_mode_max_results(*, mode: str) -> int:
    max_results = int(getattr(settings, "YT_RECENT_N_FOR_METRICS", 15))
    if mode == "full":
        max_results = int(getattr(settings, "BASELINE_N", 30))
    return max(1, int(max_results))


def _resolve_tiktok_handle(competitor: Competitor) -> str:
    handle = (competitor.handle or "").strip()
    if not handle:
        handle = str((competitor.meta or {}).get("profile_handle") or "").strip()
    if not handle and competitor.url:
        handle = str(competitor.url.rstrip("/").split("/")[-1]).lstrip("@")
    if not handle:
        handle = competitor.external_id
    return handle.lstrip("@").strip()


def _resolve_instagram_lookup(competitor: Competitor) -> str:
    lookup = (competitor.handle or "").strip()
    if not lookup:
        lookup = (competitor.url or "").strip()
    if not lookup:
        lookup = competitor.external_id.strip()
    return lookup


def _prefetch_provider_data_for_competitors(
    *,
    competitors: list[Competitor],
    mode: str,
    provider_fetch_cache: ProviderFetchCache,
) -> None:
    if not competitors:
        return

    tiktok_handles: list[str] = []
    seen_tiktok: set[str] = set()
    for competitor in competitors:
        if competitor.platform != Platform.TIKTOK:
            continue
        handle = _resolve_tiktok_handle(competitor)
        if not handle or handle in seen_tiktok or provider_fetch_cache.get_tiktok_feed(handle=handle) is not None:
            continue
        seen_tiktok.add(handle)
        tiktok_handles.append(handle)
    if tiktok_handles and provider_fetch_cache.get_platform_error(platform=Platform.TIKTOK) is None:
        try:
            grouped = fetch_tiktok_profile_feeds_cached(
                handles=tiktok_handles,
                results_per_page=_collection_mode_max_results(mode=mode),
                purpose=provider_fetch_cache.purpose,
                context_id=provider_fetch_cache.context_id,
            )
            for handle, items in grouped.items():
                if items:
                    provider_fetch_cache.store_tiktok_feed(handle=handle, items=items)
        except PlatformOnboardingError as exc:
            provider_fetch_cache.mark_platform_error(platform=Platform.TIKTOK, reason=str(exc))

    instagram_lookups: list[str] = []
    seen_instagram: set[str] = set()
    for competitor in competitors:
        if competitor.platform != Platform.INSTAGRAM:
            continue
        lookup = _resolve_instagram_lookup(competitor)
        if (
            not lookup
            or lookup in seen_instagram
            or provider_fetch_cache.get_instagram_profile(lookup=lookup) is not None
        ):
            continue
        seen_instagram.add(lookup)
        instagram_lookups.append(lookup)
    if instagram_lookups and provider_fetch_cache.get_platform_error(platform=Platform.INSTAGRAM) is None:
        try:
            profiles = fetch_instagram_profiles_cached(
                inputs=instagram_lookups,
                purpose=provider_fetch_cache.purpose,
                context_id=provider_fetch_cache.context_id,
            )
            for lookup in instagram_lookups:
                profile = _find_instagram_profile_for_lookup(profiles, lookup)
                if isinstance(profile, dict) and profile:
                    provider_fetch_cache.store_instagram_profile(profile=profile, lookups=[lookup])
        except PlatformOnboardingError as exc:
            provider_fetch_cache.mark_platform_error(platform=Platform.INSTAGRAM, reason=str(exc))


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


def build_report_preview(
    *,
    user: TgUser,
    period_start,
    period_end,
    provider_fetch_cache: ProviderFetchCache | None = None,
) -> ReportPreview:
    competitors = get_active_competitors(user=user)
    provider_fetch_cache = _ensure_provider_fetch_cache(
        provider_fetch_cache=provider_fetch_cache,
        purpose="report_collection",
    )
    _prefetch_provider_data_for_competitors(
        competitors=competitors,
        mode="incremental",
        provider_fetch_cache=provider_fetch_cache,
    )

    updated_items = []
    platform_notes: dict[str, str] = {}
    blocked_platforms: set[str] = set()
    for competitor in competitors:
        if competitor.platform in blocked_platforms:
            continue
        try:
            updated_items.extend(
                refresh_competitor(
                    competitor=competitor,
                    mode="incremental",
                    captured_at=period_end,
                    provider_fetch_cache=provider_fetch_cache,
                )
            )
        except Exception as exc:
            reason = _short_reason(str(exc))
            platform_notes.setdefault(competitor.platform, reason)
            logger.warning(
                "Skipping competitor during report preview due to refresh error "
                "(user_id=%s competitor_id=%s platform=%s external_id=%s): %s",
                user.id,
                competitor.id,
                competitor.platform,
                competitor.external_id,
                exc,
            )
            if provider_fetch_cache.get_platform_error(platform=competitor.platform):
                blocked_platforms.add(competitor.platform)
            continue

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

    payload = build_report_payload(
        scored=scored,
        period_start=period_start,
        period_end=period_end,
        baseline_by_competitor_id=baseline_by_competitor_id,
        platform_notes=platform_notes,
    )
    text = render_report_text(payload=payload, timezone_str=user.timezone_str)
    section_counts = {
        str(section.get("platform")): len(section.get("items") or []) for section in (payload.get("sections") or [])
    }
    return ReportPreview(payload=payload, text=text, section_counts=section_counts)


def build_setup_verification_preview(
    *,
    user: TgUser,
    period_end,
    provider_fetch_cache: ProviderFetchCache | None = None,
) -> ReportPreview:
    competitors = get_active_competitors(user=user)
    provider_fetch_cache = _ensure_provider_fetch_cache(
        provider_fetch_cache=provider_fetch_cache,
        purpose="setup_verification",
    )
    _prefetch_provider_data_for_competitors(
        competitors=competitors,
        mode="full",
        provider_fetch_cache=provider_fetch_cache,
    )
    section_entries: dict[str, list[dict[str, Any]]] = {
        Platform.YOUTUBE: [],
        Platform.TIKTOK: [],
        Platform.INSTAGRAM: [],
    }

    for competitor in competitors:
        try:
            refreshed_items = refresh_competitor(
                competitor=competitor,
                mode="full",
                captured_at=period_end,
                provider_fetch_cache=provider_fetch_cache,
            )
            entry = _build_setup_competitor_entry(competitor=competitor, refreshed_items=refreshed_items)
        except Exception as exc:
            entry = _build_failed_setup_competitor_entry(competitor=competitor, reason=str(exc))
        section_entries[competitor.platform].append(entry)

    payload = build_setup_verification_payload(
        generated_at=period_end,
        sections=[
            {"platform": platform, "entries": section_entries[platform][:SETUP_REPORT_MAX_COMPETITORS_PER_PLATFORM]}
            for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM)
        ],
    )
    text = render_report_text(payload=payload, timezone_str=user.timezone_str)
    section_counts = {
        platform: sum(1 for entry in entries if not str(entry.get("reason") or "").strip())
        for platform, entries in section_entries.items()
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
    provider_fetch_cache: ProviderFetchCache | None = None,
    before_send: Callable[[], None] | None = None,
) -> SentReportResult:
    preview = build_report_preview(
        user=user,
        period_start=period_start,
        period_end=period_end,
        provider_fetch_cache=provider_fetch_cache,
    )
    if required_platforms:
        assert_required_platform_sections(preview=preview, required_platforms=required_platforms)
    if before_send is not None:
        before_send()

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


def create_and_send_setup_verification_report(
    *,
    user: TgUser,
    period_start,
    period_end,
    provider_fetch_cache: ProviderFetchCache | None = None,
    before_send: Callable[[], None] | None = None,
) -> SentReportResult:
    preview = build_setup_verification_preview(
        user=user,
        period_end=period_end,
        provider_fetch_cache=provider_fetch_cache,
    )
    if before_send is not None:
        before_send()
    report = Report.objects.create(
        user=user,
        period_start=period_start,
        period_end=period_end,
        status=ReportStatus.CREATED,
        payload=preview.payload,
    )
    message_results = [send_message(chat_id=int(user.tg_chat_id), text=chunk) for chunk in split_telegram_text(text=preview.text)]
    telegram_result = {
        "message_id": message_results[0]["message_id"],
        "message_ids": [result["message_id"] for result in message_results],
    }

    report.status = ReportStatus.SENT
    report.sent_at = timezone.now()
    report.save(update_fields=["status", "sent_at"])
    return SentReportResult(report=report, preview=preview, telegram_result=telegram_result)


def _build_failed_setup_competitor_entry(*, competitor: Competitor, reason: str) -> dict[str, Any]:
    return {
        "competitor": {
            "id": competitor.id,
            "display_name": competitor.display_name,
            "handle": competitor.handle,
            "external_id": competitor.external_id,
            "url": competitor.url,
        },
        "reason": _short_reason(reason),
    }


def _build_setup_competitor_entry(*, competitor: Competitor, refreshed_items: list[ContentItem]) -> dict[str, Any]:
    eligible_items = [item for item in refreshed_items if _is_setup_short_form_item(item)]
    if not eligible_items:
        return _build_failed_setup_competitor_entry(
            competitor=competitor,
            reason="нет подходящих коротких видео для базовой проверки",
        )

    baseline = _compute_setup_baseline_metrics(competitor=competitor, fallback_items=eligible_items)
    if baseline is None:
        return _build_failed_setup_competitor_entry(
            competitor=competitor,
            reason="не хватило метрик для базовой проверки",
        )

    latest_item = max(eligible_items, key=lambda item: item.published_at)
    latest_snapshot = MetricSnapshot.objects.filter(content_item=latest_item).order_by("-captured_at").first()
    if latest_snapshot is None:
        return _build_failed_setup_competitor_entry(
            competitor=competitor,
            reason="не найден снимок метрик для последнего короткого видео",
        )

    latest_age_hours = max((latest_snapshot.captured_at - latest_item.published_at).total_seconds() / 3600.0, 0.0)
    expected_views = baseline["avg_views_per_hour"] * latest_age_hours
    expected_reactions = baseline["avg_reactions_per_hour"] * latest_age_hours
    latest_reactions = _reaction_total(latest_snapshot)

    return {
        "competitor": {
            "id": competitor.id,
            "display_name": competitor.display_name,
            "handle": competitor.handle,
            "external_id": competitor.external_id,
            "url": competitor.url,
        },
        "avg_views_per_hour": baseline["avg_views_per_hour"],
        "avg_reactions_per_hour": baseline["avg_reactions_per_hour"],
        "avg_er": baseline["avg_er"],
        "avg_virality": baseline["avg_virality"],
        "latest_item": {
            "title": latest_item.title,
            "url": latest_item.url,
            "views": int(latest_snapshot.views),
            "avg_views_same_age": int(round(expected_views)),
            "views_delta_pct": _delta_pct(float(latest_snapshot.views), expected_views),
            "reactions": latest_reactions,
            "avg_reactions_same_age": int(round(expected_reactions)),
            "reactions_delta_pct": _delta_pct(float(latest_reactions), expected_reactions),
        },
    }


def _compute_setup_baseline_metrics(*, competitor: Competitor, fallback_items: list[ContentItem]) -> dict[str, float] | None:
    window_days = int(getattr(settings, "BASELINE_WINDOW_DAYS", 30))
    n_items = int(getattr(settings, "BASELINE_N", 30))
    window_start = timezone.now() - timedelta(days=window_days)
    items = list(
        ContentItem.objects.filter(competitor=competitor, published_at__gte=window_start)
        .order_by("-published_at")
        .all()[:n_items]
    )
    eligible_items = [item for item in items if _is_setup_short_form_item(item)] or list(fallback_items)

    vph_values: list[float] = []
    rph_values: list[float] = []
    er_values: list[float] = []
    for item in eligible_items:
        snap = MetricSnapshot.objects.filter(content_item=item).order_by("-captured_at").first()
        if snap is None:
            continue
        age_hours = (snap.captured_at - item.published_at).total_seconds() / 3600.0
        if age_hours <= 0:
            continue
        vph_values.append(float(snap.views) / age_hours)
        reactions = _reaction_total(snap)
        rph_values.append(float(reactions) / age_hours)
        if snap.views > 0:
            er_values.append(float(reactions) / float(snap.views))

    if not vph_values:
        return None

    avg_vph = mean(vph_values)
    avg_rph = mean(rph_values) if rph_values else 0.0
    avg_er = mean(er_values) if er_values else 0.0
    virality_values = []
    for idx, vph in enumerate(vph_values):
        v_component = vph / max(avg_vph, 1e-6)
        if idx < len(rph_values) and avg_rph > 0:
            r_component = rph_values[idx] / max(avg_rph, 1e-6)
            virality_values.append((0.75 * v_component) + (0.25 * r_component))
        else:
            virality_values.append(v_component)
    return {
        "avg_views_per_hour": avg_vph,
        "avg_reactions_per_hour": avg_rph,
        "avg_er": avg_er,
        "avg_virality": mean(virality_values) if virality_values else 0.0,
    }


def _reaction_total(snapshot: MetricSnapshot) -> int:
    total = 0
    if snapshot.likes is not None:
        total += int(snapshot.likes)
    if snapshot.comments is not None:
        total += int(snapshot.comments)
    if snapshot.shares is not None:
        total += int(snapshot.shares)
    return total


def _delta_pct(actual: float, baseline: float) -> float:
    if baseline <= 0:
        return 0.0
    return ((actual - baseline) / baseline) * 100.0


def _short_reason(reason: str) -> str:
    text = " ".join(str(reason or "").split()).strip()
    if len(text) <= 180:
        return text
    return text[:177] + "..."


def _is_setup_short_form_item(item: ContentItem) -> bool:
    meta = item.meta if isinstance(item.meta, dict) else {}
    content_type = str(meta.get("content_type") or "").strip().lower()
    if item.platform == Platform.YOUTUBE:
        return content_type == "short"
    if item.platform == Platform.TIKTOK:
        return content_type == "video"
    if item.platform == Platform.INSTAGRAM:
        item_url = str(item.url or "").lower()
        return content_type == "reel" or "/reel/" in item_url or "/reels/" in item_url
    return False
