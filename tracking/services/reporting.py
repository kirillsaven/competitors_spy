from __future__ import annotations

from datetime import datetime

from common.time import format_dt_local

from tracking.services.scoring import ScoredItem


def build_report_payload(*, scored: list[ScoredItem], period_start: datetime, period_end: datetime) -> dict:
    youtube_items: list[dict] = []
    for s in scored[:5]:
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
                "delta_views": s.delta_views,
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
        for idx, it in enumerate(yt_items, start=1):
            title = it.get("title") or "Без названия"
            url = it.get("url") or ""
            delta = it.get("delta_views")
            score = it.get("score")
            competitor = (it.get("competitor") or {}).get("display_name") or (it.get("competitor") or {}).get("handle") or ""
            lines.append(f"{idx}) {title}")
            if competitor:
                lines.append(f"Канал: {competitor}")
            if delta is not None:
                lines.append(f"+{delta} просмотров")
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
