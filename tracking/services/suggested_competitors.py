from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass
from statistics import mean
from typing import Any

from django.conf import settings
from django.utils import timezone

from tracking.models import AddedBy, Platform, Report, ReportStatus, TgUser, UserCompetitor
from tracking.services.competitor_service import (
    get_active_user_competitor_counts,
    get_inactive_user_competitor_external_ids,
    upsert_competitor,
)
from tracking.services.platform_onboarding import youtube_profile_recent_shorts_gate_status
from tracking.services.seed_resolver import resolve_exact_seed
from tracking.services.setup_runtime import SetupRunContext


@dataclass(frozen=True)
class SuggestedCompetitorActivationResult:
    status: str
    counts: dict[str, int]
    competitor_external_id: str
    display_name: str


def _base_acceptance_payload() -> dict[str, Any]:
    return {
        "clicked_add": 0,
        "added": 0,
        "already_active": 0,
        "events": [],
    }


def _active_external_ids(*, user: TgUser, platform: str) -> set[str]:
    return {
        str(external_id).strip()
        for external_id in UserCompetitor.objects.filter(
            user=user,
            is_active=True,
            competitor__platform=platform,
        ).values_list("competitor__external_id", flat=True)
        if str(external_id).strip()
    }


_QUALITY_FACTOR_KEYS = (
    "niche_phrase_match",
    "query_phrase_match",
    "query_specificity_bonus",
    "instructional_markers",
    "adaptable_format_markers",
    "multi_query_support",
)


def _supporting_video_evidence(candidate: dict[str, Any]) -> dict[str, Any]:
    factors = dict(candidate.get("supplemental_ranking_factors") or {})
    views = int(candidate.get("views") or 0)
    likes = int(candidate.get("likes") or 0)
    comments = int(candidate.get("comments") or 0)
    reaction_rate = (likes + comments) / views if views > 0 else 0.0
    quality_signal_count = sum(1 for key in _QUALITY_FACTOR_KEYS if float(factors.get(key) or 0.0) > 0.0)
    return {
        "video_id": str(candidate.get("video_id") or "").strip(),
        "video_title": str(candidate.get("title") or "").strip(),
        "video_url": str(candidate.get("url") or "").strip(),
        "supplemental_score": float(candidate.get("supplemental_score") or 0.0),
        "views": views,
        "likes": likes,
        "comments": comments,
        "hit_count": int(candidate.get("hit_count") or 0),
        "supplemental_survival_reason": str(candidate.get("supplemental_survival_reason") or "").strip(),
        "supplemental_ranking_factors": factors,
        "reaction_rate": round(float(reaction_rate), 4),
        "quality_signal_count": int(quality_signal_count),
    }


def _supporting_video_passes_quality_gate(evidence: dict[str, Any]) -> bool:
    min_support_score = float(
        getattr(settings, "REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MIN_SUPPORTING_VIDEO_SCORE", 0.6) or 0.6
    )
    min_reaction_rate = float(
        getattr(settings, "REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MIN_SUPPORTING_VIDEO_REACTION_RATE", 0.025) or 0.025
    )
    min_traction = float(
        getattr(settings, "REPORT_YOUTUBE_SUGGESTED_COMPETITORS_MIN_SUPPORTING_VIDEO_TRACTION", 0.12) or 0.12
    )
    score = float(evidence.get("supplemental_score") or 0.0)
    reaction_rate = float(evidence.get("reaction_rate") or 0.0)
    traction = float((evidence.get("supplemental_ranking_factors") or {}).get("traction") or 0.0)
    quality_signal_count = int(evidence.get("quality_signal_count") or 0)
    return (
        score >= min_support_score
        and quality_signal_count >= 1
        and (reaction_rate >= min_reaction_rate or traction >= min_traction)
    )


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
        "suggestions_considered": 0,
        "suggestions_generated": 0,
        "suggestions_sent": 0,
        "dropped_missing_channel_id": 0,
        "dropped_blocked": 0,
        "dropped_already_active": 0,
        "dropped_not_repeated": 0,
        "dropped_dedup": 0,
        "dropped_limit": 0,
        "suppressed_by_run_cap": 0,
        "suppressed_by_cooldown": 0,
        "dropped_low_average_score": 0,
        "dropped_weak_supporting_evidence": 0,
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
    blocked_channel_ids = get_inactive_user_competitor_external_ids(user=user, platform=Platform.YOUTUBE)

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
                "supporting_videos": [],
            },
        )
        creator["current_run_hits"] += 1
        creator["current_scores"].append(float(candidate.get("supplemental_score") or 0.0))
        creator["supporting_videos"].append(_supporting_video_evidence(candidate))
        if not creator["sample_video_title"]:
            creator["sample_video_title"] = str(candidate.get("title") or "").strip()
        if not creator["sample_video_url"]:
            creator["sample_video_url"] = str(candidate.get("url") or "").strip()
        creator["matched_queries"].update(str(query).strip() for query in list(candidate.get("matched_queries") or []) if str(query).strip())

    diagnostics["current_unique_creators"] = len(current_creator_stats)
    valid_candidates = diagnostics["current_candidates"] - diagnostics["dropped_missing_channel_id"]
    diagnostics["suggestions_considered"] = len(current_creator_stats)
    diagnostics["dropped_dedup"] = max(valid_candidates - diagnostics["suggestions_considered"], 0)

    history_by_channel: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
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
            evidence = _supporting_video_evidence(candidate)
            previous_evidence = history_by_channel[channel_id].get(report.id)
            if previous_evidence is None or float(evidence.get("supplemental_score") or 0.0) > float(
                previous_evidence.get("supplemental_score") or 0.0
            ):
                history_by_channel[channel_id][report.id] = evidence

    suggestions: list[dict[str, Any]] = []
    for channel_id, creator in current_creator_stats.items():
        if channel_id in blocked_channel_ids:
            diagnostics["dropped_blocked"] += 1
            continue
        if channel_id in active_channel_ids:
            diagnostics["dropped_already_active"] += 1
            continue

        history_evidence = list(history_by_channel.get(channel_id, {}).values())
        history_scores = [float(item.get("supplemental_score") or 0.0) for item in history_evidence]
        appearance_count = 1 + len(history_scores)
        if appearance_count < min_appearances:
            diagnostics["dropped_not_repeated"] += 1
            continue

        current_best_score = max(creator["current_scores"] or [0.0])
        average_score = mean([current_best_score, *history_scores])
        if average_score < min_average_score:
            diagnostics["dropped_low_average_score"] += 1
            continue
        strongest_support = max(
            list(creator["supporting_videos"]) + history_evidence,
            key=lambda item: (
                float(item.get("supplemental_score") or 0.0),
                float(item.get("reaction_rate") or 0.0),
                float((item.get("supplemental_ranking_factors") or {}).get("traction") or 0.0),
            ),
            default={},
        )
        if not _supporting_video_passes_quality_gate(strongest_support):
            diagnostics["dropped_weak_supporting_evidence"] += 1
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
                "strongest_supporting_video_id": str(strongest_support.get("video_id") or "").strip(),
                "strongest_supporting_video_title": str(strongest_support.get("video_title") or "").strip(),
                "strongest_supporting_video_url": str(strongest_support.get("video_url") or "").strip(),
                "strongest_supporting_video_score": round(float(strongest_support.get("supplemental_score") or 0.0), 4),
                "strongest_supporting_video_reaction_rate": round(float(strongest_support.get("reaction_rate") or 0.0), 4),
                "strongest_supporting_video_traction": round(
                    float((strongest_support.get("supplemental_ranking_factors") or {}).get("traction") or 0.0),
                    4,
                ),
                "suggestion_reason": "repeated_supplemental_creator",
            }
        )

    diagnostics["suggestions_generated"] = len(suggestions)
    suggestions.sort(
        key=lambda item: (
            -int(item.get("appearance_count") or 0),
            -float(item.get("average_supplemental_score") or 0.0),
            -float(item.get("current_best_supplemental_score") or 0.0),
            str(item.get("channel_title") or "").lower(),
        )
    )
    diagnostics["dropped_limit"] = max(len(suggestions) - max_items, 0)
    suggestions = suggestions[:max_items]
    diagnostics["final_suggestions"] = len(suggestions)

    return {
        "source": "supplemental_topic_video_suggestion",
        "diagnostics": diagnostics,
        "items": suggestions,
        "section": section,
        "acceptance": _base_acceptance_payload(),
    }


def build_instagram_suggested_competitors_payload(
    *,
    user: TgUser,
    current_section_payload: dict | None,
) -> dict[str, Any]:
    max_items = max(
        1,
        int(getattr(settings, "REPORT_INSTAGRAM_SUGGESTED_COMPETITORS_MAX_ITEMS", 3) or 3),
    )
    history_runs = max(
        1,
        int(getattr(settings, "REPORT_INSTAGRAM_SUGGESTED_COMPETITORS_HISTORY_RUNS", 5) or 5),
    )
    min_appearances = max(
        2,
        int(getattr(settings, "REPORT_INSTAGRAM_SUGGESTED_COMPETITORS_MIN_APPEARANCES", 2) or 2),
    )
    min_average_score = float(
        getattr(settings, "REPORT_INSTAGRAM_SUGGESTED_COMPETITORS_MIN_AVERAGE_SCORE", 0.0) or 0.0
    )
    diagnostics: dict[str, Any] = {
        "current_candidates": 0,
        "current_unique_creators": 0,
        "history_reports_considered": 0,
        "suggestions_considered": 0,
        "suggestions_generated": 0,
        "suggestions_sent": 0,
        "dropped_missing_competitor_id": 0,
        "dropped_blocked": 0,
        "dropped_already_active": 0,
        "dropped_not_repeated": 0,
        "dropped_dedup": 0,
        "dropped_limit": 0,
        "suppressed_by_run_cap": 0,
        "suppressed_by_cooldown": 0,
        "dropped_low_average_score": 0,
        "final_suggestions": 0,
    }
    section = {
        "title": "Новые конкуренты в Instagram:",
        "subtitle": "Профили, которые стабильно попадают в топ отчета.",
        "max_items": max_items,
    }
    current_items = [
        item
        for item in list((current_section_payload or {}).get("items") or [])
        if isinstance(item, dict)
    ]
    diagnostics["current_candidates"] = len(current_items)
    if not current_items:
        return {
            "source": "instagram_report_competitor_suggestion",
            "diagnostics": diagnostics,
            "items": [],
            "section": section,
        }

    active_external_ids = _active_external_ids(user=user, platform=Platform.INSTAGRAM)
    blocked_external_ids = get_inactive_user_competitor_external_ids(user=user, platform=Platform.INSTAGRAM)

    current_stats: dict[str, dict[str, Any]] = {}
    for item in current_items:
        competitor = dict(item.get("competitor") or {})
        competitor_external_id = str(competitor.get("external_id") or "").strip()
        if not competitor_external_id:
            diagnostics["dropped_missing_competitor_id"] += 1
            continue
        creator = current_stats.setdefault(
            competitor_external_id,
            {
                "competitor_external_id": competitor_external_id,
                "competitor_display_name": str(
                    competitor.get("display_name") or competitor.get("handle") or competitor_external_id
                ).strip(),
                "competitor_handle": str(competitor.get("handle") or "").strip(),
                "competitor_url": str(item.get("competitor_url") or "").strip()
                or str(competitor.get("url") or "").strip(),
                "current_run_hits": 0,
                "current_scores": [],
                "current_virality": [],
                "sample_item_title": "",
                "sample_item_url": "",
            },
        )
        creator["current_run_hits"] += 1
        creator["current_scores"].append(float(item.get("score") or 0.0))
        creator["current_virality"].append(float(item.get("virality") or 0.0))
        if not creator["sample_item_title"]:
            creator["sample_item_title"] = str(item.get("title") or "").strip()
        if not creator["sample_item_url"]:
            creator["sample_item_url"] = str(item.get("url") or "").strip()

    diagnostics["current_unique_creators"] = len(current_stats)
    valid_candidates = diagnostics["current_candidates"] - diagnostics["dropped_missing_competitor_id"]
    diagnostics["suggestions_considered"] = len(current_stats)
    diagnostics["dropped_dedup"] = max(valid_candidates - diagnostics["suggestions_considered"], 0)

    history_by_external_id: dict[str, dict[int, dict[str, float]]] = defaultdict(dict)
    prior_reports = list(
        Report.objects.filter(user=user, status=ReportStatus.SENT).order_by("-id")[:history_runs]
    )
    diagnostics["history_reports_considered"] = len(prior_reports)
    for report in prior_reports:
        sections = [section for section in list((report.payload or {}).get("sections") or []) if isinstance(section, dict)]
        instagram_section = next(
            (section for section in sections if str(section.get("platform") or "").strip() == Platform.INSTAGRAM),
            None,
        )
        if not isinstance(instagram_section, dict):
            continue
        for item in list(instagram_section.get("items") or []):
            if not isinstance(item, dict):
                continue
            competitor = dict(item.get("competitor") or {})
            competitor_external_id = str(competitor.get("external_id") or "").strip()
            if not competitor_external_id:
                continue
            evidence = {
                "score": float(item.get("score") or 0.0),
                "virality": float(item.get("virality") or 0.0),
            }
            previous = history_by_external_id[competitor_external_id].get(report.id)
            if previous is None or evidence["score"] > float(previous.get("score") or 0.0):
                history_by_external_id[competitor_external_id][report.id] = evidence

    suggestions: list[dict[str, Any]] = []
    for competitor_external_id, creator in current_stats.items():
        if competitor_external_id in blocked_external_ids:
            diagnostics["dropped_blocked"] += 1
            continue
        if competitor_external_id in active_external_ids:
            diagnostics["dropped_already_active"] += 1
            continue
        history_scores = [
            float(evidence.get("score") or 0.0)
            for evidence in list(history_by_external_id.get(competitor_external_id, {}).values())
        ]
        history_virality = [
            float(evidence.get("virality") or 0.0)
            for evidence in list(history_by_external_id.get(competitor_external_id, {}).values())
        ]
        appearance_count = 1 + len(history_scores)
        if appearance_count < min_appearances:
            diagnostics["dropped_not_repeated"] += 1
            continue
        current_best_score = max(creator["current_scores"] or [0.0])
        average_score = mean([current_best_score, *history_scores])
        if average_score < min_average_score:
            diagnostics["dropped_low_average_score"] += 1
            continue
        current_best_virality = max(creator["current_virality"] or [0.0])
        average_virality = mean([current_best_virality, *history_virality]) if history_virality else current_best_virality
        suggestions.append(
            {
                "competitor_external_id": competitor_external_id,
                "competitor_display_name": creator["competitor_display_name"],
                "competitor_handle": creator["competitor_handle"],
                "competitor_url": creator["competitor_url"],
                "appearance_count": appearance_count,
                "history_appearance_count": len(history_scores),
                "current_run_hits": int(creator["current_run_hits"] or 0),
                "average_score": round(float(average_score), 4),
                "current_best_score": round(float(current_best_score), 4),
                "average_virality": round(float(average_virality), 4),
                "current_best_virality": round(float(current_best_virality), 4),
                "sample_item_title": creator["sample_item_title"],
                "sample_item_url": creator["sample_item_url"],
                "suggestion_reason": "repeated_report_competitor",
            }
        )

    diagnostics["suggestions_generated"] = len(suggestions)
    suggestions.sort(
        key=lambda item: (
            -int(item.get("appearance_count") or 0),
            -float(item.get("average_score") or 0.0),
            -float(item.get("current_best_score") or 0.0),
            str(item.get("competitor_display_name") or "").lower(),
        )
    )
    diagnostics["dropped_limit"] = max(len(suggestions) - max_items, 0)
    suggestions = suggestions[:max_items]
    diagnostics["final_suggestions"] = len(suggestions)

    return {
        "source": "instagram_report_competitor_suggestion",
        "diagnostics": diagnostics,
        "items": suggestions,
        "section": section,
        "acceptance": _base_acceptance_payload(),
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


def render_instagram_suggested_competitors_text(*, suggestion_payload: dict | None) -> str | None:
    if not isinstance(suggestion_payload, dict):
        return None
    items = [item for item in list(suggestion_payload.get("items") or []) if isinstance(item, dict)]
    if not items:
        return None

    section = dict(suggestion_payload.get("section") or {})
    lines = [str(section.get("title") or "Новые конкуренты в Instagram:")]
    subtitle = str(section.get("subtitle") or "").strip()
    if subtitle:
        lines.append(subtitle)
    lines.append("")
    for idx, item in enumerate(items, start=1):
        profile_name = str(
            item.get("competitor_display_name") or item.get("competitor_handle") or item.get("competitor_external_id") or "Instagram"
        ).strip()
        appearance_count = int(item.get("appearance_count") or 0)
        lines.append(f"{idx}) {profile_name}")
        lines.append(f"Появлялся в отчетах {appearance_count} раз(а).")
        lines.append("")
    lines.append("Нажми «Добавить», если хочешь включить профиль в отслеживание.")
    return "\n".join(lines).rstrip()


def record_youtube_suggested_competitor_acceptance(
    *,
    report: Report,
    suggestion: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    payload = dict(report.payload or {})
    suggested_payload = dict(payload.get("suggested_competitors") or {})
    youtube_payload = dict(suggested_payload.get("youtube") or {})
    acceptance = _base_acceptance_payload()
    acceptance.update(dict(youtube_payload.get("acceptance") or {}))
    events = [item for item in list(acceptance.get("events") or []) if isinstance(item, dict)]

    acceptance["clicked_add"] = int(acceptance.get("clicked_add") or 0) + 1
    if status in {"added", "reactivated"}:
        acceptance["added"] = int(acceptance.get("added") or 0) + 1
    elif status == "already_active":
        acceptance["already_active"] = int(acceptance.get("already_active") or 0) + 1

    events.append(
        {
            "clicked_at": timezone.now().isoformat(),
            "status": status,
            "counted_status": "added" if status in {"added", "reactivated"} else status,
            "channel_id": str(suggestion.get("channel_id") or "").strip(),
            "channel_title": str(suggestion.get("channel_title") or "").strip(),
            "suggestion_source": str(youtube_payload.get("source") or "").strip(),
            "suggestion_reason": str(suggestion.get("suggestion_reason") or "").strip(),
            "appearance_count": int(suggestion.get("appearance_count") or 0),
        }
    )
    acceptance["events"] = events[-20:]

    youtube_payload["acceptance"] = acceptance
    suggested_payload["youtube"] = youtube_payload
    payload["suggested_competitors"] = suggested_payload
    report.payload = payload
    report.save(update_fields=["payload"])
    return acceptance


def record_instagram_suggested_competitor_acceptance(
    *,
    report: Report,
    suggestion: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    payload = dict(report.payload or {})
    suggested_payload = dict(payload.get("suggested_competitors") or {})
    instagram_payload = dict(suggested_payload.get("instagram") or {})
    acceptance = _base_acceptance_payload()
    acceptance.update(dict(instagram_payload.get("acceptance") or {}))
    events = [item for item in list(acceptance.get("events") or []) if isinstance(item, dict)]

    acceptance["clicked_add"] = int(acceptance.get("clicked_add") or 0) + 1
    if status in {"added", "reactivated"}:
        acceptance["added"] = int(acceptance.get("added") or 0) + 1
    elif status == "already_active":
        acceptance["already_active"] = int(acceptance.get("already_active") or 0) + 1

    events.append(
        {
            "clicked_at": timezone.now().isoformat(),
            "status": status,
            "counted_status": "added" if status in {"added", "reactivated"} else status,
            "competitor_external_id": str(suggestion.get("competitor_external_id") or "").strip(),
            "competitor_display_name": str(suggestion.get("competitor_display_name") or "").strip(),
            "suggestion_source": str(instagram_payload.get("source") or "").strip(),
            "suggestion_reason": str(suggestion.get("suggestion_reason") or "").strip(),
            "appearance_count": int(suggestion.get("appearance_count") or 0),
        }
    )
    acceptance["events"] = events[-20:]

    instagram_payload["acceptance"] = acceptance
    suggested_payload["instagram"] = instagram_payload
    payload["suggested_competitors"] = suggested_payload
    report.payload = payload
    report.save(update_fields=["payload"])
    return acceptance


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


def activate_instagram_suggested_competitor(
    *,
    user: TgUser,
    suggestion: dict[str, Any],
    added_by: str = AddedBy.SUGGESTED,
) -> SuggestedCompetitorActivationResult:
    competitor_external_id = str(suggestion.get("competitor_external_id") or "").strip()
    if not competitor_external_id:
        raise RuntimeError("В подсказке нет competitor_external_id")

    existing_link = (
        UserCompetitor.objects.select_related("competitor")
        .filter(
            user=user,
            competitor__platform=Platform.INSTAGRAM,
            competitor__external_id=competitor_external_id,
        )
        .first()
    )
    if existing_link is not None and existing_link.is_active:
        return SuggestedCompetitorActivationResult(
            status="already_active",
            counts=get_active_user_competitor_counts(user=user),
            competitor_external_id=competitor_external_id,
            display_name=str(
                existing_link.competitor.display_name or existing_link.competitor.handle or competitor_external_id
            ),
        )

    competitor_url = str(suggestion.get("competitor_url") or "").strip()
    competitor_handle = str(suggestion.get("competitor_handle") or "").strip().lstrip("@")
    if not competitor_url and competitor_handle:
        competitor_url = f"https://www.instagram.com/{competitor_handle}/"
    competitor_display_name = str(
        suggestion.get("competitor_display_name") or suggestion.get("competitor_handle") or competitor_external_id
    ).strip()

    competitor = upsert_competitor(
        user=user,
        platform=Platform.INSTAGRAM,
        external_id=competitor_external_id,
        handle=competitor_handle or None,
        url=competitor_url,
        display_name=competitor_display_name or None,
        added_by=added_by,
        meta=None,
    )
    counts = get_active_user_competitor_counts(user=user)
    return SuggestedCompetitorActivationResult(
        status="reactivated" if existing_link is not None else "added",
        counts=counts,
        competitor_external_id=str(competitor.external_id or competitor_external_id),
        display_name=str(competitor.display_name or competitor.handle or competitor_external_id),
    )
