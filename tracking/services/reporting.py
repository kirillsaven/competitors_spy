from __future__ import annotations

import re
from datetime import datetime

from common.time import format_dt_local, format_timezone_label

from tracking.models import Platform
from tracking.services.scoring import ScoredItem


TELEGRAM_TEXT_LIMIT = 4096

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
                "shares_end": s.shares_end,
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


def build_setup_verification_payload(*, generated_at: datetime, sections: list[dict]) -> dict:
    return {
        "report_kind": "setup_verification",
        "generated_at": generated_at.isoformat(),
        "sections": sections,
    }


def render_report_text(*, payload: dict, timezone_str: str) -> str:
    if str(payload.get("report_kind") or "") == "setup_verification":
        return render_setup_verification_text(payload=payload)

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
    yt_items = (sections_by_platform.get(Platform.YOUTUBE) or {}).get("items") or []
    _render_platform_section(
        lines=lines,
        title="YouTube:",
        items=yt_items,
        timezone_str=timezone_str,
        intro="Скор: рост просмотров относительно обычного для канала (плюс вовлеченность ER).",
    )

    tiktok_items = (sections_by_platform.get(Platform.TIKTOK) or {}).get("items") or []
    _render_platform_section(
        lines=lines,
        title="TikTok:",
        items=tiktok_items,
        timezone_str=timezone_str,
        empty_line="За этот период ничего не выбилось выше обычного.",
    )

    instagram_items = (sections_by_platform.get(Platform.INSTAGRAM) or {}).get("items") or []
    _render_platform_section(
        lines=lines,
        title="Instagram:",
        items=instagram_items,
        timezone_str=timezone_str,
        empty_line="За этот период ничего не выбилось выше обычного.",
    )
    return "\n".join(lines).rstrip() + "\n"


def split_telegram_text(*, text: str, max_len: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current.rstrip() + "\n")
            current = ""
        if len(block) <= max_len:
            current = block
            continue
        partial = ""
        for line in block.splitlines():
            candidate_line = line if not partial else f"{partial}\n{line}"
            if len(candidate_line) <= max_len:
                partial = candidate_line
                continue
            if partial:
                chunks.append(partial.rstrip() + "\n")
                partial = ""
            if len(line) <= max_len:
                partial = line
                continue
            start = 0
            while start < len(line):
                piece = line[start : start + max_len]
                start += max_len
                if len(piece) == max_len:
                    chunks.append(piece.rstrip() + "\n")
                else:
                    partial = piece
        current = partial
    if current:
        chunks.append(current.rstrip() + "\n")
    return chunks


def render_setup_verification_text(*, payload: dict) -> str:
    generated_raw = payload.get("generated_at")
    generated_at = datetime.fromisoformat(generated_raw.replace("Z", "+00:00")) if isinstance(generated_raw, str) else None
    lines = ["Проверка настройки завершена"]
    if generated_at is not None:
        lines.append(f"Время: {generated_at.isoformat()}")

    sections = {
        str(section.get("platform") or ""): section
        for section in (payload.get("sections") or [])
        if isinstance(section, dict)
    }
    for platform in PLATFORM_SECTION_ORDER:
        entries = list((sections.get(platform) or {}).get("entries") or [])
        if not entries:
            continue
        lines.append("")
        lines.append(f"{_platform_title(platform)}:")
        for idx, entry in enumerate(entries, start=1):
            lines.extend(_render_setup_competitor_block(index=idx, entry=entry))

    return "\n".join(lines).rstrip() + "\n"


def _platform_title(platform: str) -> str:
    return {
        Platform.YOUTUBE: "YouTube",
        Platform.TIKTOK: "TikTok",
        Platform.INSTAGRAM: "Instagram",
    }.get(str(platform or ""), str(platform or "Platform"))


def _render_setup_competitor_block(*, index: int, entry: dict) -> list[str]:
    competitor = entry.get("competitor") or {}
    latest_item = entry.get("latest_item") or {}
    lines = [f"{index}) {competitor.get('display_name') or competitor.get('handle') or competitor.get('external_id') or 'Без названия'}"]
    account_url = competitor.get("url") or ""
    if account_url:
        lines.append(str(account_url))
    reason = str(entry.get("reason") or "").strip()
    if reason:
        lines.append(f"Причина: {reason}")
        lines.append("")
        return lines

    lines.append(
        "Среднее: "
        f"{_format_decimal(entry.get('avg_views_per_hour'))} views/h | "
        f"{_format_decimal(entry.get('avg_reactions_per_hour'))} reactions/h | "
        f"ER {_format_percent(entry.get('avg_er'))} | "
        f"virality {_format_decimal(entry.get('avg_virality'))}x"
    )
    lines.append(f"Последнее: {_clean_setup_title(str(latest_item.get('title') or 'Без названия'))}")
    item_url = latest_item.get("url") or ""
    if item_url:
        lines.append(str(item_url))
    lines.append(
        f"Просмотры: {_format_int(latest_item.get('views'))} vs {_format_int(latest_item.get('avg_views_same_age'))} "
        f"({_format_delta_pct(latest_item.get('views_delta_pct'))})"
    )
    lines.append(
        f"Реакции: {_format_int(latest_item.get('reactions'))} vs {_format_int(latest_item.get('avg_reactions_same_age'))} "
        f"({_format_delta_pct(latest_item.get('reactions_delta_pct'))})"
    )
    lines.append("")
    return lines


def _clean_setup_title(title: str) -> str:
    text = re.sub(r"(?<!\S)#[^\s#]+", "", str(title or "")).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return text or "Без названия"


def _format_decimal(value: object) -> str:
    try:
        return f"{float(value):.1f}"
    except Exception:
        return "0.0"


def _format_percent(value: object) -> str:
    try:
        return f"{float(value) * 100.0:.1f}%"
    except Exception:
        return "0.0%"


def _format_delta_pct(value: object) -> str:
    try:
        return f"{float(value):+.1f}%"
    except Exception:
        return "0.0%"


def _format_int(value: object) -> str:
    try:
        return str(int(value))
    except Exception:
        return "0"


def _render_platform_section(
    *,
    lines: list[str],
    title: str,
    items: list[dict],
    timezone_str: str,
    intro: str | None = None,
    empty_line: str | None = None,
) -> None:
    lines.append(title)
    if intro:
        lines.append(intro)
    if not items:
        if empty_line:
            lines.append(empty_line)
        return

    for idx, it in enumerate(items, start=1):
        title_text = it.get("title") or "Без названия"
        url = it.get("url") or ""
        delta = it.get("delta_views")
        delta_hours = it.get("delta_hours")
        views_end = it.get("views_end")
        likes_end = it.get("likes_end")
        comments_end = it.get("comments_end")
        shares_end = it.get("shares_end")
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
        lines.append(f"{idx}) {title_text}{tag}")
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
        try:
            if shares_end is not None:
                parts.append(f"репосты: {int(shares_end)}")
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
