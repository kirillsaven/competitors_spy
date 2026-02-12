from __future__ import annotations

from datetime import datetime

from common.time import format_dt_local

from tracking.services.scoring import ScoredItem


def build_report_payload(*, scored: list[ScoredItem], period_start: datetime, period_end: datetime) -> dict:
    youtube_items: list[dict] = []
    for s in scored[:5]:
        content_type = (s.content_item.meta or {}).get("content_type") if isinstance(s.content_item.meta, dict) else None
        youtube_items.append(
            {
                "platform": "youtube",
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
            {"platform": "youtube", "items": youtube_items},
            {"platform": "tiktok", "items": []},
            {"platform": "instagram", "items": []},
        ],
    }


def render_report_text(*, payload: dict, timezone_str: str) -> str:
    ps = payload.get("period_start")
    pe = payload.get("period_end")
    period_start = datetime.fromisoformat(ps.replace("Z", "+00:00")) if isinstance(ps, str) else None
    period_end = datetime.fromisoformat(pe.replace("Z", "+00:00")) if isinstance(pe, str) else None

    lines: list[str] = []
    if period_start and period_end:
        lines.append(
            f"Отчет за период: {format_dt_local(period_start, timezone_str)} - {format_dt_local(period_end, timezone_str)} ({timezone_str})"
        )
    else:
        lines.append("Отчет")

    # YouTube
    lines.append("")
    lines.append("YouTube:")
    yt = None
    for sec in payload.get("sections") or []:
        if sec.get("platform") == "youtube":
            yt = sec
            break
    yt_items = (yt or {}).get("items") or []
    if not yt_items:
        lines.append("Пока нет данных (попробуйте позже).")
    else:
        fallback_count = 0
        for idx, it in enumerate(yt_items, start=1):
            title = it.get("title") or "Без названия"
            url = it.get("url") or ""
            delta = it.get("delta_views")
            delta_hours = it.get("delta_hours")
            views_end = it.get("views_end")
            velocity = it.get("velocity_vph")
            score_type = it.get("score_type") or ""
            score = it.get("score")
            content_type = it.get("content_type") or ""
            competitor = (it.get("competitor") or {}).get("display_name") or (it.get("competitor") or {}).get("handle") or ""
            tag = " [Shorts]" if str(content_type) == "short" else ""
            lines.append(f"{idx}) {title}{tag}")
            if competitor:
                lines.append(f"Канал: {competitor}")
            if delta is not None:
                if delta_hours:
                    try:
                        lines.append(f"+{int(delta)} просмотров за {float(delta_hours):.1f}ч")
                    except Exception:
                        lines.append(f"+{delta} просмотров")
                else:
                    lines.append(f"+{delta} просмотров")
            else:
                fallback_count += 1
                if velocity is not None:
                    try:
                        lines.append(f"~{float(velocity):.0f} просмотров/ч (пока нет дельты)")
                    except Exception:
                        lines.append("Пока нет дельты по просмотрам")
                else:
                    lines.append("Пока нет дельты по просмотрам")
                if views_end is not None:
                    try:
                        lines.append(f"Всего просмотров: {int(views_end)}")
                    except Exception:
                        pass
            if score is not None:
                try:
                    lines.append(f"score: {float(score):.2f}")
                except Exception:
                    pass
            if url:
                lines.append(url)
            lines.append("")

    lines.append("TikTok: MVP: пока не поддерживается")
    lines.append("Instagram: MVP: пока не поддерживается")
    return "\n".join(lines).rstrip() + "\n"
