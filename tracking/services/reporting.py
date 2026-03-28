from __future__ import annotations

import re
from datetime import datetime

from common.time import format_dt_local, format_timezone_label

from tracking.models import MetricSnapshot, Platform
from tracking.services.scoring import ScoredItem


TELEGRAM_TEXT_LIMIT = 4096

PLATFORM_SECTION_ORDER = [
    Platform.YOUTUBE,
    Platform.TIKTOK,
    Platform.INSTAGRAM,
]


def build_report_payload(
    *,
    scored: list[ScoredItem],
    period_start: datetime,
    period_end: datetime,
    baseline_by_competitor_id: dict[int, object] | None = None,
    platform_notes: dict[str, str] | None = None,
) -> dict:
    section_items: dict[str, list[dict]] = {platform: [] for platform in PLATFORM_SECTION_ORDER}
    for s in scored:
        platform = str(s.content_item.platform or s.competitor.platform or "")
        if platform not in section_items or len(section_items[platform]) >= 5:
            continue
        content_type = (s.content_item.meta or {}).get("content_type") if isinstance(s.content_item.meta, dict) else None
        reactions_end = _reaction_total(
            likes=getattr(s, "likes_end", None),
            comments=getattr(s, "comments_end", None),
            shares=getattr(s, "shares_end", None),
        )
        avg_views_same_age = getattr(s, "avg_views_same_age", None)
        avg_reactions_same_age = getattr(s, "avg_reactions_same_age", None)
        views_delta_pct = getattr(s, "views_delta_pct", None)
        reactions_delta_pct = getattr(s, "reactions_delta_pct", None)
        virality = getattr(s, "virality", None)
        baseline = (baseline_by_competitor_id or {}).get(int(s.competitor.id))
        if baseline is not None and getattr(s.content_item, "id", None):
            snapshot = (
                MetricSnapshot.objects.filter(content_item_id=s.content_item.id, captured_at__lte=period_end)
                .order_by("-captured_at")
                .first()
            )
            if snapshot is not None:
                age_hours = max((snapshot.captured_at - s.content_item.published_at).total_seconds() / 3600.0, 0.0)
                avg_views_same_age = int(round(float(getattr(baseline, "vph_median", 0.0) or 0.0) * age_hours))
                avg_reactions_same_age = int(round(float(getattr(baseline, "rph_median", 0.0) or 0.0) * age_hours))
                views_delta_pct = _delta_pct(float(s.views_end or 0), float(avg_views_same_age or 0))
                reactions_delta_pct = _delta_pct(float(reactions_end or 0), float(avg_reactions_same_age or 0))
                baseline_vph = float(getattr(baseline, "vph_median", 0.0) or 0.0)
                virality = float(s.velocity or 0.0) / baseline_vph if baseline_vph > 0 else 0.0
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
                "reactions_end": reactions_end,
                "velocity_vph": s.velocity,
                "score_type": s.score_type,
                "score": s.score,
                "er_end": s.er_end,
                "avg_views_same_age": avg_views_same_age,
                "avg_reactions_same_age": avg_reactions_same_age,
                "views_delta_pct": views_delta_pct,
                "reactions_delta_pct": reactions_delta_pct,
                "virality": virality,
            }
        )

    return {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "sections": [
            {
                "platform": platform,
                "items": section_items[platform],
                **({"note": str((platform_notes or {}).get(platform) or "").strip()} if (platform_notes or {}).get(platform) else {}),
            }
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
        return render_setup_verification_text(payload=payload, timezone_str=timezone_str)

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
    yt_note = str((sections_by_platform.get(Platform.YOUTUBE) or {}).get("note") or "").strip()
    _render_platform_section(lines=lines, title="YouTube:", items=yt_items, note=yt_note)

    tiktok_items = (sections_by_platform.get(Platform.TIKTOK) or {}).get("items") or []
    tiktok_note = str((sections_by_platform.get(Platform.TIKTOK) or {}).get("note") or "").strip()
    _render_platform_section(lines=lines, title="TikTok:", items=tiktok_items, note=tiktok_note)

    instagram_items = (sections_by_platform.get(Platform.INSTAGRAM) or {}).get("items") or []
    instagram_note = str((sections_by_platform.get(Platform.INSTAGRAM) or {}).get("note") or "").strip()
    _render_platform_section(lines=lines, title="Instagram:", items=instagram_items, note=instagram_note)
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


def render_setup_verification_text(*, payload: dict, timezone_str: str) -> str:
    generated_raw = payload.get("generated_at")
    generated_at = datetime.fromisoformat(generated_raw.replace("Z", "+00:00")) if isinstance(generated_raw, str) else None
    lines = ["Проверка настройки завершена"]
    if generated_at is not None:
        lines.append(f"Время: {format_dt_local(generated_at, timezone_str)}")

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
    lines.append(f"Последнее: {_clean_title(str(latest_item.get('title') or 'Без названия'))}")
    item_url = latest_item.get("url") or ""
    if item_url:
        lines.append(str(item_url))
    lines.append(
        f"Просмотры: {_format_int(latest_item.get('views'))} vs {_format_int(latest_item.get('avg_views_same_age'))} среднее за тот же период "
        f"({_format_delta_pct(latest_item.get('views_delta_pct'))})"
    )
    lines.append(
        f"Реакции: {_format_int(latest_item.get('reactions'))} vs {_format_int(latest_item.get('avg_reactions_same_age'))} среднее за тот же период "
        f"({_format_delta_pct(latest_item.get('reactions_delta_pct'))})"
    )
    lines.append("")
    return lines


def _clean_title(title: str) -> str:
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
    note: str = "",
) -> None:
    lines.append(title)
    if note:
        lines.append(f"Причина: {note}")
    if not items:
        lines.append("Нет подходящих роликов.")
        return

    for idx, it in enumerate(items, start=1):
        title_text = _clean_title(str(it.get("title") or "Без названия"))
        url = it.get("url") or ""
        competitor = it.get("competitor") or {}
        channel_name = (
            str(competitor.get("display_name") or "").strip()
            or str(competitor.get("handle") or "").strip()
            or str(competitor.get("external_id") or "").strip()
            or "Без названия"
        )
        views_end = it.get("views_end")
        avg_views_same_age = it.get("avg_views_same_age")
        reactions_end = it.get("reactions_end")
        avg_reactions_same_age = it.get("avg_reactions_same_age")
        lines.append(f"{idx}) {channel_name}")
        lines.append(title_text)
        if url:
            lines.append(url)
        lines.append(
            f"Просмотры: {_format_int(views_end)} vs {_format_int(avg_views_same_age)} "
            f"({_format_delta_pct(it.get('views_delta_pct'))})"
        )
        lines.append(
            f"Реакции: {_format_int(reactions_end)} vs {_format_int(avg_reactions_same_age)} "
            f"({_format_delta_pct(it.get('reactions_delta_pct'))})"
        )
        lines.append(f"ER: {_format_percent(it.get('er_end'))}")
        lines.append(f"Вирусность: {_format_decimal(it.get('virality'))}x")
        lines.append("")


def _reaction_total(*, likes: object, comments: object, shares: object) -> int:
    total = 0
    for value in (likes, comments, shares):
        try:
            if value is not None:
                total += int(value)
        except Exception:
            continue
    return total


def _delta_pct(actual: float, baseline: float) -> float:
    if baseline <= 0:
        return 0.0
    return ((actual - baseline) / baseline) * 100.0
