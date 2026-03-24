from __future__ import annotations

from datetime import datetime

from common.time import format_dt_local, format_timezone_label

from tracking.models import Platform
from tracking.services.scoring import ScoredItem


PLATFORM_SECTION_ORDER = [
    Platform.YOUTUBE,
    Platform.TIKTOK,
    Platform.INSTAGRAM,
]


def build_report_payload(*, scored: list[ScoredItem], period_start: datetime, period_end: datetime) -> dict:
    section_items: dict[str, list[dict]] = {platform: [] for platform in PLATFORM_SECTION_ORDER}
    for s in scored:
        platform = str(s.content_item.platform or s.competitor.platform or "")
        if platform not in section_items or len(section_items[platform]) >= 5:
            continue
        content_type = (s.content_item.meta or {}).get("content_type") if isinstance(s.content_item.meta, dict) else None
        section_items[platform].append(
            {
                "platform": platform,
                "video_id": s.content_item.external_id,
                "title": s.content_item.title,
                "url": s.content_item.url,
                "competitor": {
                    "id": s.competitor.id,
                    "display_name": s.competitor.display_name,
                    "handle": s.competitor.handle,
                    "external_id": s.competitor.external_id,
                },
                "published_at": s.content_item.published_at.isoformat(),
                "content_type": content_type,
                "delta_views": s.delta_views,
                "delta_hours": s.delta_hours,
                "views_end": s.views_end,
                "likes_end": s.likes_end,
                "comments_end": s.comments_end,
                "velocity_vph": s.velocity,
                "score_type": s.score_type,
                "score": s.score,
                "er_end": s.er_end,
            }
        )

    return {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "sections": [
            {"platform": platform, "items": section_items[platform]}
            for platform in PLATFORM_SECTION_ORDER
        ],
    }


def render_report_text(*, payload: dict, timezone_str: str) -> str:
    ps = payload.get("period_start")
    pe = payload.get("period_end")
    period_start = datetime.fromisoformat(ps.replace("Z", "+00:00")) if isinstance(ps, str) else None
    period_end = datetime.fromisoformat(pe.replace("Z", "+00:00")) if isinstance(pe, str) else None

    lines: list[str] = []
    if period_start and period_end:
        tz_label = format_timezone_label(timezone_str)
        lines.append(f"Отчет за период: {format_dt_local(period_start, timezone_str)} - {format_dt_local(period_end, timezone_str)}")
        lines.append(f"Время: {tz_label}")
    else:
        lines.append("Отчет")

    sections_by_platform = {
        str(sec.get("platform")): sec for sec in (payload.get("sections") or []) if isinstance(sec, dict)
    }

    lines.append("")
    lines.append("YouTube:")
    lines.append("Скор: рост просмотров относительно обычного для канала (плюс вовлеченность ER).")
    yt_items = (sections_by_platform.get(Platform.YOUTUBE) or {}).get("items") or []
    if not yt_items:
        lines.append("За этот период ничего не выбилось выше обычного.")
    else:
        for idx, it in enumerate(yt_items, start=1):
            title = it.get("title") or "Без названия"
            url = it.get("url") or ""
            delta = it.get("delta_views")
            delta_hours = it.get("delta_hours")
            views_end = it.get("views_end")
            likes_end = it.get("likes_end")
            comments_end = it.get("comments_end")
            score = it.get("score")
            content_type = it.get("content_type") or ""
            competitor = (it.get("competitor") or {}).get("display_name") or (it.get("competitor") or {}).get("handle") or ""
            published_at = None
            pa = it.get("published_at")
            if isinstance(pa, str) and pa:
                try:
                    published_at = datetime.fromisoformat(pa.replace("Z", "+00:00"))
                except Exception:
                    published_at = None
            tag = " [Shorts]" if str(content_type) == "short" else ""
            lines.append(f"{idx}) {title}{tag}")
            if competitor:
                lines.append(f"Канал: {competitor}")
            if published_at:
                lines.append(f"Опубликовано: {format_dt_local(published_at, timezone_str)}")

            if delta is not None and delta_hours:
                try:
                    delta_int = int(delta)
                    views_end_int = int(views_end) if views_end is not None else None
                    growth = ""
                    if views_end_int is not None:
                        views_start_int = views_end_int - delta_int
                        if views_start_int > 0:
                            growth = f" ({(float(delta_int) / float(views_start_int)) * 100.0:+.1f}%)"
                    lines.append(f"Просмотры за период: +{delta_int}{growth} (за {float(delta_hours):.1f}ч)")
                except Exception:
                    lines.append(f"Просмотры за период: +{delta}")
            else:
                lines.append("Просмотры за период: пока нет (нужен предыдущий сбор)")

            if views_end is not None:
                try:
                    lines.append(f"Всего просмотров: {int(views_end)}")
                except Exception:
                    pass

            parts: list[str] = []
            try:
                if likes_end is not None:
                    parts.append(f"лайки: {int(likes_end)}")
            except Exception:
                pass
            try:
                if comments_end is not None:
                    parts.append(f"комментарии: {int(comments_end)}")
            except Exception:
                pass
            er = it.get("er_end")
            if er is not None:
                try:
                    parts.append(f"ER: {float(er) * 100.0:.2f}%")
                except Exception:
                    pass
            if parts:
                lines.append("Реакции: " + ", ".join(parts))

            if score is not None:
                try:
                    lines.append(f"Вирусность: {float(score):.2f}")
                except Exception:
                    pass
            if url:
                lines.append(url)
            lines.append("")

    lines.append("TikTok: MVP: пока не поддерживается")
    lines.append("Instagram: MVP: пока не поддерживается")
    return "\n".join(lines).rstrip() + "\n"
