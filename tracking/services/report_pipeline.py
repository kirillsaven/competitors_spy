from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from django.conf import settings
from django.utils import timezone

from botapp.keyboards import kb_youtube_suggested_competitors
from botapp.telegram_api import send_message
from tracking.models import (
    Competitor,
    ContentItem,
    MetricSnapshot,
    Platform,
    Report,
    ReportStatus,
    SeedProfile,
    TgUser,
    UserLinkedAccount,
    UserCompetitor,
)
from tracking.services.collector import refresh_competitor
from tracking.services.platform_onboarding import (
    PlatformOnboardingError,
    _find_instagram_profile_for_lookup,
    fetch_instagram_profiles_cached,
    fetch_tiktok_profile_feeds_cached,
    youtube_profile_recent_shorts_gate_status,
)
from tracking.services.provider_runtime import ProviderFetchCache
from tracking.services.report_filters import (
    filter_content_items_for_stopwords,
    filter_scored_items_for_stopwords,
    get_user_report_stopwords,
)
from tracking.services.suggested_competitors import (
    build_youtube_suggested_competitors_payload,
    render_youtube_suggested_competitors_text,
)
from tracking.services.reporting import (
    build_report_payload,
    build_setup_verification_payload,
    render_report_text,
    split_telegram_text,
)
from tracking.services.scoring import build_adaptation_context, compute_competitor_baseline, score_items_for_period
from tracking.services.youtube_service import YouTubeNotConfigured
from tracking.services.youtube_topic_video_collection import collect_youtube_topic_video_candidates

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


@dataclass(frozen=True)
class PreparedYoutubeSuggestionDelivery:
    items_to_send: list[dict[str, Any]]
    message_cap: int
    cooldown_hours: int
    suppressed_by_run_cap: int
    suppressed_by_cooldown: int
    suppressed_items: list[dict[str, Any]]


SETUP_REPORT_MAX_COMPETITORS_PER_PLATFORM = 20


def _empty_platform_diagnostics() -> dict[str, Any]:
    return {
        "active_competitors": 0,
        "gate_rejected_competitors": 0,
        "no_short_form_competitors": 0,
        "youtube_uploads_pages_scanned": 0,
        "youtube_uploads_inspected": 0,
        "youtube_short_form_items_found": 0,
        "youtube_usable_short_form_items_returned": 0,
        "refreshed_items": 0,
        "scored_items": 0,
        "strict_items": 0,
        "fallback_candidates": 0,
        "fallback_items": 0,
        "fallback_rejected_by_already_reported": 0,
        "fallback_rejected_by_stopwords": 0,
        "fallback_reasons_used": {},
        "dropped_by_age": 0,
        "dropped_by_min_views": 0,
        "dropped_by_delta_threshold": 0,
        "dropped_by_already_reported": 0,
        "dropped_by_stopwords": 0,
        "final_items": 0,
        "empty_reason": None,
    }


def _init_platform_diagnostics() -> dict[str, dict[str, Any]]:
    return {platform: _empty_platform_diagnostics() for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM)}


def _is_no_short_form_reason(*, platform: str, reason: str) -> bool:
    normalized = str(reason or "").lower()
    if platform == Platform.YOUTUBE:
        return "no recent shorts with usable metrics" in normalized
    if platform == Platform.INSTAGRAM:
        return "no recent reels with views" in normalized
    return False


def _classify_item_drop_reason(
    *,
    item: ContentItem,
    competitor_by_item_id: dict[int, Competitor],
    baseline_by_competitor_id: dict[int, object],
    period_start,
    period_end,
) -> str | None:
    if item.platform == Platform.YOUTUBE and not _is_setup_short_form_item(item):
        return "other"

    max_age_days = int(getattr(settings, "REPORT_MAX_ITEM_AGE_DAYS", 60))
    min_published_at = period_end - timedelta(days=max_age_days)
    if item.published_at < min_published_at:
        return "age"

    competitor = competitor_by_item_id.get(item.id)
    if competitor is None:
        return "other"
    if baseline_by_competitor_id.get(competitor.id) is None:
        return "other"

    snap_end = (
        MetricSnapshot.objects.filter(content_item=item, captured_at__lte=period_end).order_by("-captured_at").first()
    )
    if snap_end is None:
        return "other"

    min_views_end = int(getattr(settings, "MIN_VIEWS_END", 1000))
    if int(snap_end.views) < min_views_end:
        return "min_views"

    snap_start = (
        MetricSnapshot.objects.filter(content_item=item, captured_at__lte=period_start).order_by("-captured_at").first()
    )
    if snap_start is None:
        return None

    dv = int(snap_end.views) - int(snap_start.views)
    dh = (snap_end.captured_at - snap_start.captured_at).total_seconds() / 3600.0
    if dv < 0 or dh <= 0:
        return None

    min_delta_views = int(getattr(settings, "MIN_DELTA_VIEWS", 500))
    short_window_fallback_hours = float(getattr(settings, "REPORT_SHORT_WINDOW_FALLBACK_HOURS", 6.0) or 6.0)
    min_delta_floor = 20
    period_hours = max((period_end - period_start).total_seconds() / 3600.0, 0.0)
    effective_min_delta = max(int(min_delta_views * (dh / 24.0)), min_delta_floor)
    if dv < effective_min_delta and period_hours > short_window_fallback_hours:
        return "delta_threshold"
    return None


def _finalize_platform_diagnostics(
    *,
    platform: str,
    diagnostics: dict[str, Any],
    platform_note: str,
) -> None:
    diagnostics["final_items"] = int(diagnostics.get("final_items") or 0)
    if diagnostics["final_items"] > 0 or platform_note:
        diagnostics["empty_reason"] = None
        return
    active_competitors = int(diagnostics.get("active_competitors") or 0)
    gate_rejected = int(diagnostics.get("gate_rejected_competitors") or 0)
    refreshed_items = int(diagnostics.get("refreshed_items") or 0)
    no_short_form_competitors = int(diagnostics.get("no_short_form_competitors") or 0)
    scored_items = int(diagnostics.get("scored_items") or 0) + int(diagnostics.get("fallback_candidates") or 0)
    dropped_by_already_reported = int(diagnostics.get("dropped_by_already_reported") or 0) + int(
        diagnostics.get("fallback_rejected_by_already_reported") or 0
    )
    dropped_by_stopwords = int(diagnostics.get("dropped_by_stopwords") or 0) + int(
        diagnostics.get("fallback_rejected_by_stopwords") or 0
    )
    remaining_after_already_reported = max(0, scored_items - dropped_by_already_reported)

    reason = None
    if active_competitors <= 0:
        reason = "no_active_competitors"
    elif platform == Platform.YOUTUBE and gate_rejected >= active_competitors and refreshed_items <= 0:
        reason = "gate_rejected_competitors"
    elif refreshed_items <= 0 and no_short_form_competitors > 0:
        reason = "no_short_form_items_found"
    elif scored_items > 0 and dropped_by_already_reported >= scored_items:
        reason = "all_filtered_by_already_reported"
    elif remaining_after_already_reported > 0 and dropped_by_stopwords >= remaining_after_already_reported:
        reason = "all_filtered_by_stopwords"
    elif refreshed_items > 0:
        reason = "all_filtered_by_scoring"
    diagnostics["empty_reason"] = reason


def _log_platform_diagnostics(*, user: TgUser, period_start, period_end, platform_diagnostics: dict[str, dict[str, Any]]) -> None:
    for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM):
        diagnostics = platform_diagnostics.get(platform) or {}
        logger.info(
            "report_platform_diagnostics user_id=%s platform=%s period_start=%s period_end=%s "
            "active=%s gate_rejected=%s yt_pages=%s yt_uploads=%s yt_shorts_found=%s yt_shorts_returned=%s "
            "refreshed=%s strict_items=%s fallback_candidates=%s fallback_items=%s "
            "fallback_rejected_history=%s fallback_rejected_stopwords=%s fallback_reasons=%s "
            "dropped_age=%s dropped_min_views=%s dropped_delta=%s "
            "dropped_already_reported=%s dropped_stopwords=%s final=%s empty_reason=%s",
            user.id,
            platform,
            period_start.isoformat(),
            period_end.isoformat(),
            diagnostics.get("active_competitors", 0),
            diagnostics.get("gate_rejected_competitors", 0),
            diagnostics.get("youtube_uploads_pages_scanned", 0),
            diagnostics.get("youtube_uploads_inspected", 0),
            diagnostics.get("youtube_short_form_items_found", 0),
            diagnostics.get("youtube_usable_short_form_items_returned", 0),
            diagnostics.get("refreshed_items", 0),
            diagnostics.get("strict_items", 0),
            diagnostics.get("fallback_candidates", 0),
            diagnostics.get("fallback_items", 0),
            diagnostics.get("fallback_rejected_by_already_reported", 0),
            diagnostics.get("fallback_rejected_by_stopwords", 0),
            diagnostics.get("fallback_reasons_used", {}),
            diagnostics.get("dropped_by_age", 0),
            diagnostics.get("dropped_by_min_views", 0),
            diagnostics.get("dropped_by_delta_threshold", 0),
            diagnostics.get("dropped_by_already_reported", 0),
            diagnostics.get("dropped_by_stopwords", 0),
            diagnostics.get("final_items", 0),
            diagnostics.get("empty_reason"),
        )


def _merge_youtube_refresh_diagnostics(
    *,
    competitor: Competitor,
    platform_diagnostics: dict[str, dict[str, Any]],
    provider_fetch_cache: ProviderFetchCache | None,
) -> None:
    if competitor.platform != Platform.YOUTUBE or provider_fetch_cache is None:
        return
    diagnostics = provider_fetch_cache.get_youtube_refresh_diagnostics(competitor_id=competitor.id)
    if not diagnostics:
        return
    platform_entry = platform_diagnostics.get(Platform.YOUTUBE)
    if platform_entry is None:
        return
    platform_entry["youtube_uploads_pages_scanned"] += int(diagnostics.get("uploads_pages_scanned") or 0)
    platform_entry["youtube_uploads_inspected"] += int(diagnostics.get("uploads_inspected") or 0)
    platform_entry["youtube_short_form_items_found"] += int(diagnostics.get("short_form_items_found") or 0)
    platform_entry["youtube_usable_short_form_items_returned"] += int(
        diagnostics.get("usable_short_form_items_returned") or 0
    )


def _load_user_adaptation_context(*, user: TgUser, competitors: list[Competitor]):
    niche_keywords = _load_user_niche_keywords(user=user)
    linked_accounts = list(UserLinkedAccount.objects.filter(user=user).all())
    return build_adaptation_context(
        niche_keywords=niche_keywords,
        linked_accounts=linked_accounts,
        competitors=competitors,
    )


def _load_user_niche_keywords(*, user: TgUser) -> list[str]:
    seed_profile = (
        SeedProfile.objects.filter(user=user)
        .exclude(niche_keywords=[])
        .order_by("-id")
        .first()
    )
    return [str(item).strip() for item in list(getattr(seed_profile, "niche_keywords", []) or []) if str(item).strip()]


def _build_supplemental_payload(
    *,
    user: TgUser,
    competitors: list[Competitor],
    period_end,
) -> dict[str, dict] | None:
    if not bool(getattr(settings, "ENABLE_YOUTUBE_SUPPLEMENTAL_TOPIC_VIDEO_COLLECTION", False)):
        return None
    niche_keywords = _load_user_niche_keywords(user=user)
    linked_accounts = list(UserLinkedAccount.objects.filter(user=user).all())
    try:
        result = collect_youtube_topic_video_candidates(
            niche_keywords=niche_keywords,
            linked_accounts=linked_accounts,
            competitors=competitors,
            now=period_end,
        )
    except YouTubeNotConfigured:
        logger.info(
            "report_supplemental_collection_skipped user_id=%s lane=youtube_topic_video reason=youtube_not_configured",
            user.id,
        )
        return None
    logger.info(
        "report_supplemental_collection user_id=%s lane=youtube_topic_video queries_built=%s queries_executed=%s final_candidates=%s",
        user.id,
        result.diagnostics.get("queries_built", 0),
        result.diagnostics.get("queries_executed", 0),
        result.diagnostics.get("final_candidates", 0),
    )
    return {"youtube_topic_video": result.to_payload()}


def _attach_suggested_competitors_payload(
    *,
    user: TgUser,
    payload: dict[str, Any],
) -> None:
    supplemental_payload = dict(payload.get("supplemental") or {})
    youtube_lane_payload = supplemental_payload.get("youtube_topic_video")
    youtube_suggestions = build_youtube_suggested_competitors_payload(
        user=user,
        current_lane_payload=youtube_lane_payload,
    )
    payload["suggested_competitors"] = {"youtube": youtube_suggestions}
    _log_youtube_suggested_competitors_observability(
        user=user,
        suggestion_payload=youtube_suggestions,
        stage="preview",
    )


def _youtube_suggestion_delivery_defaults() -> dict[str, Any]:
    return {
        "message_cap": 1,
        "cooldown_hours": 72,
        "sent_items": [],
        "suppressed_items": [],
    }


def _set_youtube_suggestion_delivery(
    *,
    payload: dict[str, Any],
    delivery: dict[str, Any],
) -> None:
    suggested_payload = dict(payload.get("suggested_competitors") or {})
    youtube_payload = dict(suggested_payload.get("youtube") or {})
    current_delivery = _youtube_suggestion_delivery_defaults()
    current_delivery.update(dict(youtube_payload.get("delivery") or {}))
    current_delivery.update(dict(delivery or {}))
    youtube_payload["delivery"] = current_delivery
    suggested_payload["youtube"] = youtube_payload
    payload["suggested_competitors"] = suggested_payload


def _set_youtube_suggestions_sent_count(*, payload: dict[str, Any], suggestions_sent: int) -> None:
    suggested_payload = dict(payload.get("suggested_competitors") or {})
    youtube_payload = dict(suggested_payload.get("youtube") or {})
    diagnostics = dict(youtube_payload.get("diagnostics") or {})
    diagnostics["suggestions_sent"] = int(suggestions_sent or 0)
    youtube_payload["diagnostics"] = diagnostics
    suggested_payload["youtube"] = youtube_payload
    payload["suggested_competitors"] = suggested_payload


def _parse_iso_datetime(raw_value: Any):
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _prepare_youtube_suggested_competitor_delivery(
    *,
    user: TgUser,
    report: Report,
) -> PreparedYoutubeSuggestionDelivery:
    message_cap = max(
        1,
        int(getattr(settings, "REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MESSAGE_CAP", 1) or 1),
    )
    cooldown_hours = max(
        1,
        int(getattr(settings, "REPORT_YOUTUBE_SUGGESTED_COMPETITORS_RESEND_COOLDOWN_HOURS", 72) or 72),
    )
    cutoff = timezone.now() - timedelta(hours=cooldown_hours)
    payload = (((report.payload or {}).get("suggested_competitors") or {}).get("youtube") or {})
    suggestions = [item for item in list(payload.get("items") or []) if isinstance(item, dict)]
    recently_sent_by_channel: dict[str, Any] = {}

    prior_reports = (
        Report.objects.filter(user=user, status=ReportStatus.SENT)
        .exclude(id=report.id)
        .order_by("-sent_at", "-id")
    )
    for prior_report in prior_reports:
        youtube_payload = ((((prior_report.payload or {}).get("suggested_competitors") or {}).get("youtube")) or {})
        delivery = dict(youtube_payload.get("delivery") or {})
        for sent_item in list(delivery.get("sent_items") or []):
            if not isinstance(sent_item, dict):
                continue
            channel_id = str(sent_item.get("channel_id") or "").strip()
            if not channel_id or channel_id in recently_sent_by_channel:
                continue
            sent_at = _parse_iso_datetime(sent_item.get("sent_at")) or prior_report.sent_at or prior_report.created_at
            if sent_at is None or sent_at < cutoff:
                continue
            recently_sent_by_channel[channel_id] = sent_at

    eligible_items: list[dict[str, Any]] = []
    suppressed_items: list[dict[str, Any]] = []
    suppressed_by_cooldown = 0
    for suggestion in suggestions:
        channel_id = str(suggestion.get("channel_id") or "").strip()
        if channel_id and channel_id in recently_sent_by_channel:
            suppressed_by_cooldown += 1
            suppressed_items.append(
                {
                    "channel_id": channel_id,
                    "channel_title": str(suggestion.get("channel_title") or channel_id).strip(),
                    "suppression_reason": "cooldown",
                    "last_sent_at": recently_sent_by_channel[channel_id].isoformat(),
                }
            )
            continue
        eligible_items.append(dict(suggestion))

    items_to_send = eligible_items[:message_cap]
    suppressed_by_run_cap = max(len(eligible_items) - len(items_to_send), 0)
    for suggestion in eligible_items[message_cap:]:
        suppressed_items.append(
            {
                "channel_id": str(suggestion.get("channel_id") or "").strip(),
                "channel_title": str(
                    suggestion.get("channel_title") or suggestion.get("channel_id") or "YouTube"
                ).strip(),
                "suppression_reason": "run_cap",
            }
        )

    return PreparedYoutubeSuggestionDelivery(
        items_to_send=items_to_send,
        message_cap=message_cap,
        cooldown_hours=cooldown_hours,
        suppressed_by_run_cap=suppressed_by_run_cap,
        suppressed_by_cooldown=suppressed_by_cooldown,
        suppressed_items=suppressed_items,
    )


def _build_youtube_suggestion_delivery_payload(
    *,
    prepared: PreparedYoutubeSuggestionDelivery,
    sent_items: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "message_cap": prepared.message_cap,
        "cooldown_hours": prepared.cooldown_hours,
        "sent_items": sent_items,
        "suppressed_items": list(prepared.suppressed_items),
    }


def _set_youtube_suggestion_guardrail_diagnostics(
    *,
    payload: dict[str, Any],
    suppressed_by_run_cap: int,
    suppressed_by_cooldown: int,
) -> None:
    suggested_payload = dict(payload.get("suggested_competitors") or {})
    youtube_payload = dict(suggested_payload.get("youtube") or {})
    diagnostics = dict(youtube_payload.get("diagnostics") or {})
    diagnostics["suppressed_by_run_cap"] = int(suppressed_by_run_cap or 0)
    diagnostics["suppressed_by_cooldown"] = int(suppressed_by_cooldown or 0)
    youtube_payload["diagnostics"] = diagnostics
    suggested_payload["youtube"] = youtube_payload
    payload["suggested_competitors"] = suggested_payload


def _log_youtube_suggested_competitors_observability(
    *,
    user: TgUser,
    suggestion_payload: dict | None,
    stage: str,
    report_id: int | None = None,
) -> None:
    payload = dict(suggestion_payload or {})
    diagnostics = dict(payload.get("diagnostics") or {})
    logger.info(
        "report_suggested_competitors_observability user_id=%s report_id=%s stage=%s "
        "suggestions_considered=%s suggestions_generated=%s suggestions_sent=%s "
        "dropped_already_active=%s dropped_not_repeated=%s dropped_dedup=%s dropped_limit=%s "
        "suppressed_by_run_cap=%s suppressed_by_cooldown=%s",
        user.id,
        report_id,
        stage,
        diagnostics.get("suggestions_considered", 0),
        diagnostics.get("suggestions_generated", 0),
        diagnostics.get("suggestions_sent", 0),
        diagnostics.get("dropped_already_active", 0),
        diagnostics.get("dropped_not_repeated", 0),
        diagnostics.get("dropped_dedup", 0),
        diagnostics.get("dropped_limit", 0),
        diagnostics.get("suppressed_by_run_cap", 0),
        diagnostics.get("suppressed_by_cooldown", 0),
    )


def _send_youtube_suggested_competitors_message(*, user: TgUser, report: Report) -> dict[str, Any]:
    suggestions_payload = (((report.payload or {}).get("suggested_competitors") or {}).get("youtube") or {})
    diagnostics = dict(suggestions_payload.get("diagnostics") or {})
    prepared = _prepare_youtube_suggested_competitor_delivery(user=user, report=report)
    diagnostics["suppressed_by_run_cap"] = prepared.suppressed_by_run_cap
    diagnostics["suppressed_by_cooldown"] = prepared.suppressed_by_cooldown
    suggestions_payload = dict(suggestions_payload)
    suggestions_payload["diagnostics"] = diagnostics
    guardrailed_payload = {**suggestions_payload, "items": list(prepared.items_to_send)}
    text = render_youtube_suggested_competitors_text(suggestion_payload=guardrailed_payload)
    if not text:
        return {
            "suggestions_sent": 0,
            "delivery": _build_youtube_suggestion_delivery_payload(prepared=prepared, sent_items=[]),
        }

    suggestions = [item for item in list(guardrailed_payload.get("items") or []) if isinstance(item, dict)]
    if not suggestions:
        return {
            "suggestions_sent": 0,
            "delivery": _build_youtube_suggestion_delivery_payload(prepared=prepared, sent_items=[]),
        }

    reply_markup = kb_youtube_suggested_competitors(report_id=report.id, suggestions=suggestions).model_dump(exclude_none=True)
    result = send_message(
        chat_id=int(user.tg_chat_id),
        text=text,
        reply_markup=reply_markup,
    )
    sent_at = timezone.now().isoformat()
    sent_items = [{**dict(item), "sent_at": sent_at, "message_id": result.get("message_id")} for item in suggestions]
    return {
        **result,
        "suggestions_sent": len(suggestions),
        "delivery": _build_youtube_suggestion_delivery_payload(prepared=prepared, sent_items=sent_items),
    }


def _log_adaptation_relevance(*, user: TgUser, scored: list[object]) -> None:
    for item in list(scored or [])[:10]:
        content_item = getattr(item, "content_item", None)
        competitor = getattr(item, "competitor", None)
        logger.info(
            "report_item_adaptation user_id=%s platform=%s item_id=%s competitor_id=%s selection_path=%s "
            "fallback_reason=%s adaptation_score=%s factors=%s",
            user.id,
            getattr(content_item, "platform", ""),
            getattr(content_item, "external_id", ""),
            getattr(competitor, "id", ""),
            getattr(item, "selection_path", "strict"),
            getattr(item, "fallback_reason", None),
            getattr(item, "adaptation_relevance_score", 0.0),
            getattr(item, "adaptation_relevance_factors", {}),
        )


def _count_scored_items_by_platform(*, scored: list[object]) -> dict[str, int]:
    counts = {platform: 0 for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM)}
    for item in scored:
        platform = str(getattr(getattr(item, "content_item", None), "platform", "") or getattr(getattr(item, "competitor", None), "platform", "")).strip()
        if platform in counts:
            counts[platform] += 1
    return counts


def _select_soft_fallback_items(
    *,
    strict_scored: list[object],
    fallback_scored: list[object],
) -> list[object]:
    min_items_per_platform = max(0, int(getattr(settings, "REPORT_FALLBACK_MIN_ITEMS_PER_PLATFORM", 2) or 2))
    max_fallback_items = max(0, int(getattr(settings, "REPORT_FALLBACK_MAX_ITEMS_PER_PLATFORM", 2) or 2))
    if min_items_per_platform <= 0 or max_fallback_items <= 0:
        return []

    strict_counts = _count_scored_items_by_platform(scored=strict_scored)
    strict_ids = {getattr(getattr(item, "content_item", None), "id", None) for item in strict_scored}
    fallback_by_platform = {platform: [] for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM)}
    for item in fallback_scored:
        item_id = getattr(getattr(item, "content_item", None), "id", None)
        if item_id in strict_ids:
            continue
        platform = str(getattr(getattr(item, "content_item", None), "platform", "") or getattr(getattr(item, "competitor", None), "platform", "")).strip()
        if platform in fallback_by_platform:
            fallback_by_platform[platform].append(item)

    selected: list[object] = []
    for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM):
        strict_count = strict_counts.get(platform, 0)
        if strict_count >= min_items_per_platform:
            continue
        allowance = min(min_items_per_platform - strict_count, max_fallback_items)
        if allowance <= 0:
            continue
        selected.extend(fallback_by_platform[platform][:allowance])
    return selected


def _reported_content_key(*, platform: str | None, external_id: str | None, url: str | None = None) -> tuple[str, str] | None:
    normalized_platform = str(platform or "").strip()
    normalized_external_id = str(external_id or "").strip()
    if normalized_platform and normalized_external_id:
        return normalized_platform, normalized_external_id
    normalized_url = str(url or "").strip()
    if normalized_platform and normalized_url:
        return normalized_platform, normalized_url
    return None


def _load_previously_reported_content_keys(*, user: TgUser) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    reports = Report.objects.filter(user=user, status=ReportStatus.SENT).order_by("id")
    for report in reports:
        payload = report.payload if isinstance(report.payload, dict) else {}
        if str(payload.get("report_kind") or "").strip() == "setup_verification":
            continue
        for section in payload.get("sections") or []:
            if not isinstance(section, dict):
                continue
            section_platform = str(section.get("platform") or "").strip()
            for item in section.get("items") or []:
                if not isinstance(item, dict):
                    continue
                key = _reported_content_key(
                    platform=str(item.get("platform") or section_platform or "").strip(),
                    external_id=str(item.get("video_id") or item.get("external_id") or "").strip(),
                    url=str(item.get("url") or "").strip(),
                )
                if key is not None:
                    keys.add(key)
    return keys


def _exclude_previously_reported_items(*, user: TgUser, scored: list[object]) -> list[object]:
    seen_keys = _load_previously_reported_content_keys(user=user)
    if not seen_keys:
        return list(scored)
    filtered: list[object] = []
    skipped = 0
    for item in scored:
        content_item = getattr(item, "content_item", None)
        competitor = getattr(item, "competitor", None)
        key = _reported_content_key(
            platform=str(getattr(content_item, "platform", None) or getattr(competitor, "platform", None) or "").strip(),
            external_id=str(getattr(content_item, "external_id", "") or "").strip(),
            url=str(getattr(content_item, "url", "") or "").strip(),
        )
        if key is not None and key in seen_keys:
            skipped += 1
            continue
        filtered.append(item)
    if skipped:
        logger.info("Excluded previously reported items for user_id=%s count=%s", user.id, skipped)
    return filtered


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
        except Exception as exc:
            logger.warning(
                "Skipping TikTok provider prefetch for report pipeline "
                "(purpose=%s handles=%s): %s",
                provider_fetch_cache.purpose,
                ",".join(tiktok_handles),
                exc,
            )
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
        except Exception as exc:
            logger.warning(
                "Skipping Instagram provider prefetch for report pipeline "
                "(purpose=%s lookups=%s): %s",
                provider_fetch_cache.purpose,
                ",".join(instagram_lookups),
                exc,
            )
            provider_fetch_cache.mark_platform_error(platform=Platform.INSTAGRAM, reason=str(exc))


def get_active_competitors(*, user: TgUser) -> list[Competitor]:
    competitors: list[Competitor] = []
    for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM):
        links = (
            UserCompetitor.objects.select_related("competitor")
            .filter(user=user, is_active=True, competitor__platform=platform)
            .order_by("id")
            .all()
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
    eligible_competitors: list[Competitor] = []
    user_stopwords = get_user_report_stopwords(user=user)
    platform_diagnostics = _init_platform_diagnostics()
    for competitor in competitors:
        platform_diagnostics[competitor.platform]["active_competitors"] += 1
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
        if competitor.platform == Platform.YOUTUBE:
            try:
                passes_gate, recent_count = youtube_profile_recent_shorts_gate_status(
                    external_id=competitor.external_id,
                    handle=competitor.handle,
                    url=competitor.url,
                    display_name=competitor.display_name,
                )
            except Exception as exc:
                reason = _short_reason(str(exc))
                platform_notes.setdefault(competitor.platform, reason)
                logger.warning(
                    "Skipping YouTube competitor during report preview due to shorts gate error "
                    "(user_id=%s competitor_id=%s external_id=%s): %s",
                    user.id,
                    competitor.id,
                    competitor.external_id,
                    exc,
                )
                continue
            if not passes_gate:
                platform_diagnostics[competitor.platform]["gate_rejected_competitors"] += 1
                logger.info(
                    "Skipping YouTube competitor during report preview due to recent shorts gate "
                    "(user_id=%s competitor_id=%s external_id=%s recent_shorts=%s)",
                    user.id,
                    competitor.id,
                    competitor.external_id,
                    recent_count,
                )
                continue
        eligible_competitors.append(competitor)
        try:
            refreshed_items = refresh_competitor(
                competitor=competitor,
                mode="incremental",
                captured_at=period_end,
                provider_fetch_cache=provider_fetch_cache,
            )
            updated_items.extend(refreshed_items)
            platform_diagnostics[competitor.platform]["refreshed_items"] += len(refreshed_items)
            _merge_youtube_refresh_diagnostics(
                competitor=competitor,
                platform_diagnostics=platform_diagnostics,
                provider_fetch_cache=provider_fetch_cache,
            )
        except Exception as exc:
            _merge_youtube_refresh_diagnostics(
                competitor=competitor,
                platform_diagnostics=platform_diagnostics,
                provider_fetch_cache=provider_fetch_cache,
            )
            reason = _short_reason(str(exc))
            if _is_no_short_form_reason(platform=competitor.platform, reason=reason):
                platform_diagnostics[competitor.platform]["no_short_form_competitors"] += 1
            else:
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
        competitor.id: compute_competitor_baseline(competitor=competitor, now=period_end)
        for competitor in eligible_competitors
    }
    adaptation_context = _load_user_adaptation_context(user=user, competitors=competitors)
    for item in updated_items:
        drop_reason = _classify_item_drop_reason(
            item=item,
            competitor_by_item_id=competitor_by_item_id,
            baseline_by_competitor_id=baseline_by_competitor_id,
            period_start=period_start,
            period_end=period_end,
        )
        if drop_reason is None:
            platform_diagnostics[item.platform]["scored_items"] += 1
            continue
        if drop_reason == "age":
            platform_diagnostics[item.platform]["dropped_by_age"] += 1
        elif drop_reason == "min_views":
            platform_diagnostics[item.platform]["dropped_by_min_views"] += 1
        elif drop_reason == "delta_threshold":
            platform_diagnostics[item.platform]["dropped_by_delta_threshold"] += 1

    scored = score_items_for_period(
        items=updated_items,
        competitor_by_item_id=competitor_by_item_id,
        baseline_by_competitor_id=baseline_by_competitor_id,
        period_start=period_start,
        period_end=period_end,
        adaptation_context=adaptation_context,
        selection_mode="strict",
    )
    strict_scored = list(scored)
    strict_scored_after_history = _exclude_previously_reported_items(user=user, scored=strict_scored)
    kept_after_history = {getattr(item.content_item, "id", None) for item in strict_scored_after_history}
    for item in strict_scored:
        item_id = getattr(item.content_item, "id", None)
        if item_id not in kept_after_history:
            platform = str(getattr(item.content_item, "platform", None) or getattr(item.competitor, "platform", "")).strip()
            if platform in platform_diagnostics:
                platform_diagnostics[platform]["dropped_by_already_reported"] += 1
    strict_scored_final = filter_scored_items_for_stopwords(scored=strict_scored_after_history, stopwords=user_stopwords)
    kept_after_stopwords = {getattr(item.content_item, "id", None) for item in strict_scored_final}
    for item in strict_scored_after_history:
        item_id = getattr(item.content_item, "id", None)
        if item_id not in kept_after_stopwords:
            platform = str(getattr(item.content_item, "platform", None) or getattr(item.competitor, "platform", "")).strip()
            if platform in platform_diagnostics:
                platform_diagnostics[platform]["dropped_by_stopwords"] += 1
    strict_counts = _count_scored_items_by_platform(scored=strict_scored_final)
    for platform, count in strict_counts.items():
        if platform in platform_diagnostics:
            platform_diagnostics[platform]["strict_items"] = count

    min_fallback_items = max(0, int(getattr(settings, "REPORT_FALLBACK_MIN_ITEMS_PER_PLATFORM", 2) or 2))
    selected_fallback: list[object] = []
    if min_fallback_items > 0 and any(count < min_fallback_items for count in strict_counts.values()):
        fallback_scored = score_items_for_period(
            items=updated_items,
            competitor_by_item_id=competitor_by_item_id,
            baseline_by_competitor_id=baseline_by_competitor_id,
            period_start=period_start,
            period_end=period_end,
            adaptation_context=adaptation_context,
            selection_mode="fallback",
        )
        for platform, count in _count_scored_items_by_platform(scored=fallback_scored).items():
            if platform in platform_diagnostics:
                platform_diagnostics[platform]["fallback_candidates"] = count

        fallback_after_history = _exclude_previously_reported_items(user=user, scored=fallback_scored)
        kept_fallback_after_history = {getattr(item.content_item, "id", None) for item in fallback_after_history}
        for item in fallback_scored:
            item_id = getattr(item.content_item, "id", None)
            if item_id not in kept_fallback_after_history:
                platform = str(getattr(item.content_item, "platform", None) or getattr(item.competitor, "platform", "")).strip()
                if platform in platform_diagnostics:
                    platform_diagnostics[platform]["fallback_rejected_by_already_reported"] += 1

        fallback_after_stopwords = filter_scored_items_for_stopwords(scored=fallback_after_history, stopwords=user_stopwords)
        kept_fallback_after_stopwords = {getattr(item.content_item, "id", None) for item in fallback_after_stopwords}
        for item in fallback_after_history:
            item_id = getattr(item.content_item, "id", None)
            if item_id not in kept_fallback_after_stopwords:
                platform = str(getattr(item.content_item, "platform", None) or getattr(item.competitor, "platform", "")).strip()
                if platform in platform_diagnostics:
                    platform_diagnostics[platform]["fallback_rejected_by_stopwords"] += 1

        selected_fallback = _select_soft_fallback_items(
            strict_scored=strict_scored_final,
            fallback_scored=fallback_after_stopwords,
        )
    scored = list(strict_scored_final) + list(selected_fallback)
    scored.sort(key=lambda item: getattr(item, "score", 0.0), reverse=True)

    for item in selected_fallback:
        platform = str(getattr(item.content_item, "platform", None) or getattr(item.competitor, "platform", "")).strip()
        if platform in platform_diagnostics:
            platform_diagnostics[platform]["fallback_items"] += 1
            fallback_reasons = dict(platform_diagnostics[platform].get("fallback_reasons_used") or {})
            reason = str(getattr(item, "fallback_reason", "") or "fallback")
            fallback_reasons[reason] = int(fallback_reasons.get(reason, 0) or 0) + 1
            platform_diagnostics[platform]["fallback_reasons_used"] = fallback_reasons

    for item in scored:
        platform = str(getattr(item.content_item, "platform", None) or getattr(item.competitor, "platform", "")).strip()
        if platform in platform_diagnostics:
            platform_diagnostics[platform]["final_items"] += 1
    _log_adaptation_relevance(user=user, scored=scored)

    for platform in (Platform.YOUTUBE, Platform.TIKTOK, Platform.INSTAGRAM):
        _finalize_platform_diagnostics(
            platform=platform,
            diagnostics=platform_diagnostics[platform],
            platform_note=str(platform_notes.get(platform) or "").strip(),
        )
    _log_platform_diagnostics(
        user=user,
        period_start=period_start,
        period_end=period_end,
        platform_diagnostics=platform_diagnostics,
    )
    supplemental_payload = _build_supplemental_payload(
        user=user,
        competitors=competitors,
        period_end=period_end,
    )

    payload = build_report_payload(
        scored=scored,
        period_start=period_start,
        period_end=period_end,
        baseline_by_competitor_id=baseline_by_competitor_id,
        platform_notes=platform_notes,
        platform_diagnostics=platform_diagnostics,
        supplemental=supplemental_payload,
    )
    _attach_suggested_competitors_payload(user=user, payload=payload)
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
    user_stopwords = get_user_report_stopwords(user=user)
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
            entry = _build_setup_competitor_entry(
                competitor=competitor,
                refreshed_items=refreshed_items,
                user_stopwords=user_stopwords,
            )
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
    try:
        message_results = [
            send_message(chat_id=int(user.tg_chat_id), text=chunk)
            for chunk in split_telegram_text(text=preview.text)
        ]
    except Exception:
        report.status = ReportStatus.FAILED
        report.save(update_fields=["status"])
        raise
    telegram_result = {
        "message_id": message_results[0]["message_id"],
        "message_ids": [result["message_id"] for result in message_results],
    }
    try:
        suggestion_message_result = _send_youtube_suggested_competitors_message(user=user, report=report)
    except Exception:
        logger.exception(
            "report_suggested_competitors_send_failed user_id=%s report_id=%s",
            user.id,
            report.id,
        )
        suggestion_message_result = {}
    suggestion_delivery = dict((suggestion_message_result or {}).get("delivery") or {})
    _set_youtube_suggestion_delivery(payload=preview.payload, delivery=suggestion_delivery)
    _set_youtube_suggestion_delivery(payload=report.payload, delivery=suggestion_delivery)
    _set_youtube_suggestion_guardrail_diagnostics(
        payload=preview.payload,
        suppressed_by_run_cap=len(
            [
                item
                for item in list(suggestion_delivery.get("suppressed_items") or [])
                if isinstance(item, dict) and str(item.get("suppression_reason") or "") == "run_cap"
            ]
        ),
        suppressed_by_cooldown=len(
            [
                item
                for item in list(suggestion_delivery.get("suppressed_items") or [])
                if isinstance(item, dict) and str(item.get("suppression_reason") or "") == "cooldown"
            ]
        ),
    )
    _set_youtube_suggestion_guardrail_diagnostics(
        payload=report.payload,
        suppressed_by_run_cap=len(
            [
                item
                for item in list(suggestion_delivery.get("suppressed_items") or [])
                if isinstance(item, dict) and str(item.get("suppression_reason") or "") == "run_cap"
            ]
        ),
        suppressed_by_cooldown=len(
            [
                item
                for item in list(suggestion_delivery.get("suppressed_items") or [])
                if isinstance(item, dict) and str(item.get("suppression_reason") or "") == "cooldown"
            ]
        ),
    )
    suggestions_sent = int((suggestion_message_result or {}).get("suggestions_sent") or 0)
    _set_youtube_suggestions_sent_count(payload=preview.payload, suggestions_sent=suggestions_sent)
    _set_youtube_suggestions_sent_count(payload=report.payload, suggestions_sent=suggestions_sent)
    _log_youtube_suggested_competitors_observability(
        user=user,
        suggestion_payload=(((report.payload or {}).get("suggested_competitors") or {}).get("youtube") or {}),
        stage="send",
        report_id=report.id,
    )
    if suggestion_message_result.get("message_id") is not None:
        telegram_result["suggestion_message_id"] = suggestion_message_result.get("message_id")
        telegram_result["suggestion_message_ids"] = [suggestion_message_result.get("message_id")]

    report.status = ReportStatus.SENT
    report.sent_at = timezone.now()
    report.save(update_fields=["status", "sent_at", "payload"])
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
    try:
        message_results = [
            send_message(chat_id=int(user.tg_chat_id), text=chunk)
            for chunk in split_telegram_text(text=preview.text)
        ]
    except Exception:
        report.status = ReportStatus.FAILED
        report.save(update_fields=["status"])
        raise
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


def _build_setup_competitor_entry(
    *,
    competitor: Competitor,
    refreshed_items: list[ContentItem],
    user_stopwords: list[str],
) -> dict[str, Any]:
    eligible_items = [item for item in refreshed_items if _is_setup_short_form_item(item)]
    if not eligible_items:
        return _build_failed_setup_competitor_entry(
            competitor=competitor,
            reason="нет подходящих коротких видео для базовой проверки",
        )
    visible_items = filter_content_items_for_stopwords(items=eligible_items, stopwords=user_stopwords)
    if not visible_items:
        return _build_failed_setup_competitor_entry(
            competitor=competitor,
            reason="все подходящие короткие видео скрыты стоп-словами пользователя",
        )

    baseline = _compute_setup_baseline_metrics(competitor=competitor, fallback_items=eligible_items)
    if baseline is None:
        return _build_failed_setup_competitor_entry(
            competitor=competitor,
            reason="не хватило метрик для базовой проверки",
        )

    latest_item = max(visible_items, key=lambda item: item.published_at)
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
