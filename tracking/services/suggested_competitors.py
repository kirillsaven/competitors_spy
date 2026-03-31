from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
from statistics import mean
from typing import Any

from django.conf import settings

from tracking.models import AddedBy, Platform, Report, ReportStatus, TgUser, UserCompetitor
from tracking.services.competitor_service import get_active_user_competitor_counts, upsert_competitor
from tracking.services.platform_onboarding import youtube_profile_recent_shorts_gate_status
from tracking.services.seed_resolver import resolve_exact_seed
from tracking.services.setup_runtime import SetupRunContext


@dataclass(frozen=True)
class SuggestedCompetitorActivationResult:
    status: str
    counts: dict[str, int]
    competitor_external_id: str
    display_name: str


def build_youtube_suggested_competitors_payload(
    *,
    user: TgUser,
    current_lane_payload: dict | None,
) -> dict[str, Any]:
    max_items = max(
        1,
        int(getattr(settings, "REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MAX_ITEMS", 3) or 3),
    )
    history_runs = max(
        1,
        int(getattr(settings, "REPORT_YOUTUBE_SUGGESTED_COMPETITORS_HISTORY_RUNS", 5) or 5),
    )
    min_appearances = max(
        2,
        int(getattr(settings, "REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MIN_APPEARANCES", 2) or 2),
    )
    min_average_score = float(
        getattr(settings, "REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MIN_AVERAGE_SCORE", 0.45) or 0.45
    )

    diagnostics: dict[str, Any] = {
        "current_candidates": 0,
        "current_unique_creators": 0,
        "history_reports_considered": 0,
        "dropped_missing_channel_id": 0,
        "dropped_already_active": 0,
        "dropped_not_repeated": 0,
        "dropped_low_average_score": 0,
        "final_suggestions": 0,
    }
    section = {
        "title": "Новые конкуренты в YouTube:",
        "subtitle": "Каналы, которые несколько раз появлялись в дополнительных идеях по теме.",
        "max_items": max_items,
    }

    candidates = [
        candidate
        for candidate in list((current_lane_payload or {}).get("candidates") or [])
        if isinstance(candidate, dict)
    ]
    diagnostics["current_candidates"] = len(candidates)
    if not candidates:
        return {
            "source": "supplemental_topic_video_suggestion",
            "diagnostics": diagnostics,
            "items": [],
            "section": section,
        }

    active_channel_ids = {
        str(external_id).strip()
        for external_id in UserCompetitor.objects.filter(
            user=user,
            is_active=True,
            competitor__platform=Platform.YOUTUBE,
        ).values_list("competitor__external_id", flat=True)
        if str(external_id).strip()
    }

    current_creator_stats: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        channel_id = str(candidate.get("channel_id") or "").strip()
        if not channel_id:
            diagnostics["dropped_missing_channel_id"] += 1
            continue
        creator = current_creator_stats.setdefault(
            channel_id,
            {
                "channel_id": channel_id,
                "channel_title": str(candidate.get("channel_title") or "").strip() or channel_id,
                "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                "current_run_hits": 0,
                "current_scores": [],
                "sample_video_title": "",
                "sample_video_url": "",
                "matched_queries": set(),
            },
        )
        creator["current_run_hits"] += 1
        creator["current_scores"].append(float(candidate.get("supplemental_score") or 0.0))
        if not creator["sample_video_title"]:
            creator["sample_video_title"] = str(candidate.get("title") or "").strip()
        if not creator["sample_video_url"]:
            creator["sample_video_url"] = str(candidate.get("url") or "").strip()
        creator["matched_queries"].update(str(query).strip() for query in list(candidate.get("matched_queries") or []) if str(query).strip())

    diagnostics["current_unique_creators"] = len(current_creator_stats)

    history_by_channel: dict[str, dict[int, float]] = defaultdict(dict)
    prior_reports = list(
        Report.objects.filter(user=user, status=ReportStatus.SENT).order_by("-id")[:history_runs]
    )
    diagnostics["history_reports_considered"] = len(prior_reports)
    for report in prior_reports:
        lane_payload = (((report.payload or {}).get("supplemental") or {}).get("youtube_topic_video") or {})
        for candidate in list(lane_payload.get("candidates") or []):
            if not isinstance(candidate, dict):
                continue
            channel_id = str(candidate.get("channel_id") or "").strip()
            if not channel_id:
                continue
            score = float(candidate.get("supplemental_score") or 0.0)
            previous_score = history_by_channel[channel_id].get(report.id)
            if previous_score is None or score > previous_score:
                history_by_channel[channel_id][report.id] = score

    suggestions: list[dict[str, Any]] = []
    for channel_id, creator in current_creator_stats.items():
        if channel_id in active_channel_ids:
            diagnostics["dropped_already_active"] += 1
            continue

        history_scores = list(history_by_channel.get(channel_id, {}).values())
        appearance_count = 1 + len(history_scores)
        if appearance_count < min_appearances:
            diagnostics["dropped_not_repeated"] += 1
            continue

        current_best_score = max(creator["current_scores"] or [0.0])
        average_score = mean([current_best_score, *history_scores])
        if average_score < min_average_score:
            diagnostics["dropped_low_average_score"] += 1
            continue

        suggestions.append(
            {
                "channel_id": channel_id,
                "channel_title": creator["channel_title"],
                "channel_url": creator["channel_url"],
                "appearance_count": appearance_count,
                "history_appearance_count": len(history_scores),
                "current_run_hits": int(creator["current_run_hits"] or 0),
                "average_supplemental_score": round(float(average_score), 4),
                "current_best_supplemental_score": round(float(current_best_score), 4),
                "matched_queries": sorted(creator["matched_queries"]),
                "sample_video_title": creator["sample_video_title"],
                "sample_video_url": creator["sample_video_url"],
                "suggestion_reason": "repeated_supplemental_creator",
            }
        )

    suggestions.sort(
        key=lambda item: (
            -int(item.get("appearance_count") or 0),
            -float(item.get("average_supplemental_score") or 0.0),
            -float(item.get("current_best_supplemental_score") or 0.0),
            str(item.get("channel_title") or "").lower(),
        )
    )
    suggestions = suggestions[:max_items]
    diagnostics["final_suggestions"] = len(suggestions)

    return {
        "source": "supplemental_topic_video_suggestion",
        "diagnostics": diagnostics,
        "items": suggestions,
        "section": section,
    }


def render_youtube_suggested_competitors_text(*, suggestion_payload: dict | None) -> str | None:
    if not isinstance(suggestion_payload, dict):
        return None
    items = [item for item in list(suggestion_payload.get("items") or []) if isinstance(item, dict)]
    if not items:
        return None

    section = dict(suggestion_payload.get("section") or {})
    lines = [str(section.get("title") or "Новые конкуренты в YouTube:")]
    subtitle = str(section.get("subtitle") or "").strip()
    if subtitle:
        lines.append(subtitle)
    lines.append("")
    for idx, item in enumerate(items, start=1):
        channel_title = str(item.get("channel_title") or item.get("channel_id") or "YouTube").strip()
        appearance_count = int(item.get("appearance_count") or 0)
        lines.append(f"{idx}) {channel_title}")
        lines.append(f"Появлялся в дополнительных идеях {appearance_count} раз(а).")
        lines.append("")
    lines.append("Нажми «Добавить», если хочешь включить канал в отслеживание.")
    return "\n".join(lines).rstrip()


def activate_youtube_suggested_competitor(
    *,
    user: TgUser,
    suggestion: dict[str, Any],
    added_by: str = AddedBy.SUGGESTED,
) -> SuggestedCompetitorActivationResult:
    channel_id = str(suggestion.get("channel_id") or "").strip()
    if not channel_id:
        raise RuntimeError("В подсказке нет channel_id")

    existing_link = (
        UserCompetitor.objects.select_related("competitor")
        .filter(
            user=user,
            competitor__platform=Platform.YOUTUBE,
            competitor__external_id=channel_id,
        )
        .first()
    )
    if existing_link is not None and existing_link.is_active:
        return SuggestedCompetitorActivationResult(
            status="already_active",
            counts=get_active_user_competitor_counts(user=user),
            competitor_external_id=channel_id,
            display_name=str(existing_link.competitor.display_name or existing_link.competitor.handle or channel_id),
        )

    channel_url = str(suggestion.get("channel_url") or "").strip() or f"https://www.youtube.com/channel/{channel_id}"
    context = SetupRunContext()
    seed = resolve_exact_seed(channel_url, context=context)
    if seed is None or seed.platform != Platform.YOUTUBE:
        raise RuntimeError("Не смог подтвердить YouTube-канал из подсказки")

    passes_gate, recent_count = youtube_profile_recent_shorts_gate_status(
        external_id=seed.external_id,
        handle=seed.handle,
        url=seed.url,
        display_name=seed.title,
        context=context,
    )
    if not passes_gate:
        raise RuntimeError(
            "YouTube-канал не прошел фильтр активности "
            f"(shorts за 60 дней: {recent_count}, нужно минимум 2)"
        )

    competitor = upsert_competitor(
        user=user,
        platform=seed.platform,
        external_id=seed.external_id,
        handle=seed.handle,
        url=seed.url,
        display_name=seed.title,
        added_by=added_by,
        meta={
            **({"description": seed.description} if seed.description else {}),
            **({"uploads_playlist_id": seed.uploads_playlist_id} if seed.uploads_playlist_id else {}),
        }
        or None,
    )
    counts = get_active_user_competitor_counts(user=user)
    return SuggestedCompetitorActivationResult(
        status="reactivated" if existing_link is not None else "added",
        counts=counts,
        competitor_external_id=str(competitor.external_id or channel_id),
        display_name=str(competitor.display_name or competitor.handle or channel_id),
    )
