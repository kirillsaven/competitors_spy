from __future__ import annotations

from datetime import datetime

from common.time import format_dt_local, format_timezone_label

from tracking.models import Platform
from tracking.services.scoring import BaselineMetrics, ScoredItem


PLATFORM_SECTION_ORDER = [
    Platform.YOUTUBE,
    Platform.TIKTOK,
    Platform.INSTAGRAM,
]
TELEGRAM_TEXT_LIMIT = 4000
SETUP_VERIFICATION_MAX_EXAMPLES = 2
SETUP_VERIFICATION_MAX_FAILURES = 2


def _platform_label(platform: str) -> str:
    return {
        Platform.YOUTUBE: "YouTube",
        Platform.TIKTOK: "TikTok",
        Platform.INSTAGRAM: "Instagram",
    }.get(str(platform or ""), str(platform or "Platform"))


def _content_type_tag(content_type: str) -> str:
    kind = str(content_type or "").strip().lower()
    if kind == "short":
        return " [Shorts]"
    if kind == "reel":
        return " [Reels]"
    return ""


def _shorten_reason(reason: str, max_len: int = 140) -> str:
    value = " ".join(str(reason or "").split()).strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."


def _baseline_metric(baseline: BaselineMetrics | dict | None, key: str):
    if baseline is None:
        return None
    if isinstance(baseline, dict):
        return baseline.get(key)
    return getattr(baseline, key, None)


def _clean_report_title(title: str) -> str:
    cleaned = " ".join(part for part in str(title or "").split() if not part.startswith("#")).strip()
    return cleaned or "Без названия"


def build_report_payload(
    *,
    scored: list[ScoredItem],
    period_start: datetime,
    period_end: datetime,
    baseline_by_competitor_id: dict[int, BaselineMetrics | dict] | None = None,
    report_reason_code: str = "",
    report_reason: str = "",
    collection_failures: list[dict] | None = None,
) -> dict:
    section_items: dict[str, list[dict]] = {platform: [] for platform in PLATFORM_SECTION_ORDER}
    baseline_by_competitor_id = baseline_by_competitor_id or {}
    for s in scored:
        platform = str(s.content_item.platform or s.competitor.platform or "")
        if platform not in section_items or len(section_items[platform]) >= 5:
            continue
        content_type = (s.content_item.meta or {}).get("content_type") if isinstance(s.content_item.meta, dict) else None
        baseline = baseline_by_competitor_id.get(int(s.competitor.id))
        elapsed_hours = max((period_end - s.content_item.published_at).total_seconds() / 3600.0, 0.0)
        avg_views = None
        vph_median = _baseline_metric(baseline, "vph_median")
        if vph_median is not None and elapsed_hours > 0:
            avg_views = float(vph_median) * elapsed_hours
        actual_reactions = sum(
            int(value)
            for value in (s.likes_end, s.comments_end, s.shares_end)
            if value is not None
        )
        avg_reactions = None
        rph_median = _baseline_metric(baseline, "rph_median")
        if rph_median is not None and elapsed_hours > 0:
            avg_reactions = float(rph_median) * elapsed_hours

        def _delta_pct(actual: float | int | None, average: float | None) -> float | None:
            if actual is None or average is None or average <= 0:
                return None
            return ((float(actual) - float(average)) / float(average)) * 100.0

        section_items[platform].append(
            {
                "platform": platform,
                "video_id": s.content_item.external_id,
                "title": _clean_report_title(s.content_item.title),
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
                "reactions_end": actual_reactions,
                "avg_views_same_window": avg_views,
                "avg_reactions_same_window": avg_reactions,
                "views_delta_pct": _delta_pct(s.views_end, avg_views),
                "reactions_delta_pct": _delta_pct(actual_reactions, avg_reactions),
                "velocity_vph": s.velocity,
                "score_type": s.score_type,
                "score": s.score,
                "er_end": s.er_end,
            }
        )

    return {
        "report_kind": "scheduled",
        "report_reason_code": str(report_reason_code or ""),
        "report_reason": str(report_reason or ""),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "sections": [
            {"platform": platform, "items": section_items[platform]}
            for platform in PLATFORM_SECTION_ORDER
        ],
        "collection_failures": list(collection_failures or []),
    }


def build_setup_verification_payload(
    *,
    generated_at: datetime,
    sections: list[dict],
) -> dict:
    return {
        "report_kind": "setup_verification",
        "generated_at": generated_at.isoformat(),
        "sections": list(sections or []),
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
    report_reason = str(payload.get("report_reason") or "").strip()
    if report_reason:
        lines.append(report_reason)
        lines.append("")
    yt_items = (sections_by_platform.get(Platform.YOUTUBE) or {}).get("items") or []
    _render_platform_section(
        lines=lines,
        title="YouTube:",
        items=yt_items,
        timezone_str=timezone_str,
        empty_line="Нет элементов для этого отчета." if report_reason else "За этот период ничего не выбилось выше обычного.",
    )

    tiktok_items = (sections_by_platform.get(Platform.TIKTOK) or {}).get("items") or []
    _render_platform_section(
        lines=lines,
        title="TikTok:",
        items=tiktok_items,
        timezone_str=timezone_str,
        empty_line="Нет элементов для этого отчета." if report_reason else "За этот период ничего не выбилось выше обычного.",
    )

    instagram_items = (sections_by_platform.get(Platform.INSTAGRAM) or {}).get("items") or []
    _render_platform_section(
        lines=lines,
        title="Instagram:",
        items=instagram_items,
        timezone_str=timezone_str,
        empty_line="Нет элементов для этого отчета." if report_reason else "За этот период ничего не выбилось выше обычного.",
    )

    failures = [failure for failure in (payload.get("collection_failures") or []) if isinstance(failure, dict)]
    if failures:
        platform_labels = {
            Platform.YOUTUBE: "YouTube",
            Platform.TIKTOK: "TikTok",
            Platform.INSTAGRAM: "Instagram",
        }
        lines.append("")
        lines.append("Проблемы при сборе:")
        for platform in PLATFORM_SECTION_ORDER:
            platform_failures = [failure for failure in failures if str(failure.get("platform") or "") == platform]
            if not platform_failures:
                continue
            lines.append(f"{platform_labels.get(platform, str(platform))}:")
            for failure in platform_failures[:5]:
                competitor = failure.get("competitor") or {}
                label = (
                    str(competitor.get("display_name") or "").strip()
                    or str(competitor.get("handle") or "").strip()
                    or str(competitor.get("id") or "").strip()
                    or "unknown"
                )
                reason = str(failure.get("reason") or "").strip() or "unknown error"
                lines.append(f"- {label}: {reason}")
    return "\n".join(lines).rstrip() + "\n"


def render_setup_verification_text(*, payload: dict, timezone_str: str) -> str:
    generated_raw = payload.get("generated_at")
    generated_at = datetime.fromisoformat(generated_raw.replace("Z", "+00:00")) if isinstance(generated_raw, str) else None
    lines: list[str] = ["Проверка настройки завершена"]
    if generated_at is not None:
        lines.append(f"Время: {format_dt_local(generated_at, timezone_str)}")

    for section in (payload.get("sections") or []):
        if not isinstance(section, dict):
            continue
        platform = str(section.get("platform") or "")
        selected = int(section.get("selected_competitors") or 0)
        successful = int(section.get("successful_competitors") or 0)
        failed = int(section.get("failed_competitors") or 0)
        lines.append("")
        lines.append(
            f"{_platform_label(platform)}: выбрано {selected}, успешно {successful}, ошибок {failed}"
        )

        examples = [example for example in (section.get("examples") or []) if isinstance(example, dict)]
        if examples:
            lines.append("Примеры последних собранных материалов:")
            for idx, example in enumerate(examples[:SETUP_VERIFICATION_MAX_EXAMPLES], start=1):
                title = str(example.get("title") or "Без названия").strip()
                tag = _content_type_tag(str(example.get("content_type") or ""))
                competitor = str(example.get("competitor") or "").strip()
                published_at = example.get("published_at")
                lines.append(f"{idx}) {title}{tag}")
                if competitor:
                    lines.append(f"Источник: {competitor}")
                if isinstance(published_at, str) and published_at:
                    try:
                        lines.append(
                            f"Опубликовано: {format_dt_local(datetime.fromisoformat(published_at.replace('Z', '+00:00')), timezone_str)}"
                        )
                    except Exception:
                        pass
                url = str(example.get("url") or "").strip()
                if url:
                    lines.append(url)
        else:
            lines.append("Примеры не добавлены: провайдер не вернул подходящие материалы в этом сборе.")

        failures = [failure for failure in (section.get("failures") or []) if isinstance(failure, dict)]
        if failures:
            lines.append("Проблемы при сборе:")
            for failure in failures[:SETUP_VERIFICATION_MAX_FAILURES]:
                competitor = str(failure.get("competitor") or "").strip() or "unknown"
                reason = _shorten_reason(str(failure.get("reason") or "").strip() or "unknown error")
                lines.append(f"- {competitor}: {reason}")

    return "\n".join(lines).rstrip() + "\n"


def split_telegram_text(*, text: str, max_len: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    value = str(text or "").strip()
    if not value:
        return []
    if len(value) <= max_len:
        return [value]

    chunks: list[str] = []
    current = ""
    for block in value.split("\n\n"):
        block_text = block.strip()
        if not block_text:
            continue
        candidate = block_text if not current else f"{current}\n\n{block_text}"
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(block_text) <= max_len:
            current = block_text
            continue
        lines = [line.rstrip() for line in block_text.splitlines() if line.strip()]
        current_line_chunk = ""
        for line in lines:
            line_candidate = line if not current_line_chunk else f"{current_line_chunk}\n{line}"
            if len(line_candidate) <= max_len:
                current_line_chunk = line_candidate
                continue
            if current_line_chunk:
                chunks.append(current_line_chunk)
                current_line_chunk = ""
            remaining = line
            while len(remaining) > max_len:
                chunks.append(remaining[:max_len])
                remaining = remaining[max_len:]
            current_line_chunk = remaining
        if current_line_chunk:
            current = current_line_chunk
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


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
        views_end = it.get("views_end")
        reactions_end = it.get("reactions_end")
        avg_views_same_window = it.get("avg_views_same_window")
        avg_reactions_same_window = it.get("avg_reactions_same_window")
        views_delta_pct = it.get("views_delta_pct")
        reactions_delta_pct = it.get("reactions_delta_pct")
        score = it.get("score")
        content_type = it.get("content_type") or ""
        tag = " [Shorts]" if str(content_type) == "short" else ""
        lines.append(f"{idx}) {title_text}{tag}")
        if url:
            lines.append(url)
        try:
            if views_end is not None:
                avg_views_text = "н/д"
                if avg_views_same_window is not None:
                    avg_views_text = f"{int(round(float(avg_views_same_window)))}"
                delta_views_text = "н/д"
                if views_delta_pct is not None:
                    delta_views_text = f"{float(views_delta_pct):+.1f}%"
                lines.append(f"Просмотры: {int(views_end)} | среднее автора: {avg_views_text} | Δ: {delta_views_text}")
        except Exception:
            pass
        try:
            if reactions_end is not None:
                avg_reactions_text = "н/д"
                if avg_reactions_same_window is not None:
                    avg_reactions_text = f"{int(round(float(avg_reactions_same_window)))}"
                delta_reactions_text = "н/д"
                if reactions_delta_pct is not None:
                    delta_reactions_text = f"{float(reactions_delta_pct):+.1f}%"
                lines.append(
                    f"Реакции: {int(reactions_end)} | среднее автора: {avg_reactions_text} | Δ: {delta_reactions_text}"
                )
        except Exception:
            pass
        er = it.get("er_end")
        if er is not None:
            try:
                lines.append(f"ER: {float(er) * 100.0:.2f}%")
            except Exception:
                pass
        if score is not None:
            try:
                lines.append(f"Вирусность: {float(score):.2f}")
            except Exception:
                pass
        lines.append("")
