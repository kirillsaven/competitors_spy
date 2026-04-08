from __future__ import annotations

from urllib.parse import urlparse

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


TIME_PRESETS = [
    "00:00",
    "02:00",
    "04:00",
    "06:00",
    "08:00",
    "10:00",
    "12:00",
    "14:00",
    "16:00",
    "18:00",
    "20:00",
    "22:00",
]

TZ_OFFSET_PRESETS = [f"{h:+03d}:00" for h in range(-12, 15)]
GLOBAL_BACK_CALLBACK = "global_back"


def _add_back_button(builder: InlineKeyboardBuilder) -> None:
    builder.row(InlineKeyboardButton(text="Назад", callback_data=GLOBAL_BACK_CALLBACK))


def _platform_chip(platform: str | None) -> str:
    return {
        "youtube": "[YT]",
        "instagram": "[IG]",
        "tiktok": "[TT]",
    }.get(str(platform or "").strip().lower(), "[?]")


def kb_back_only() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    _add_back_button(b)
    return b.as_markup()


def kb_seed_candidates(*, candidates: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for idx, c in enumerate((candidates or [])[:8]):
        platform_chip = _platform_chip(c.get("platform"))
        title = (c.get("title") or "").strip()
        handle = (c.get("handle") or "").strip()
        subs = c.get("subscriber_count")
        if not title:
            title = handle or (c.get("external_id") or "")
        txt = f"{platform_chip} {title}".strip()
        if handle:
            txt = f"{txt} (@{handle})"
        try:
            subs_i = int(subs) if subs is not None else 0
        except Exception:
            subs_i = 0
        if subs_i > 0:
            txt = f"{txt} - {subs_i:,}".replace(",", " ")
        buttons = [InlineKeyboardButton(text=txt[:64], callback_data=f"seed_pick:{idx}")]
        safe_url = _safe_http_url(c.get("url"))
        if safe_url:
            buttons.append(InlineKeyboardButton(text="↗", url=safe_url))
        b.row(*buttons)
    b.row(InlineKeyboardButton(text="Это не то", callback_data="seed_retry"))
    _add_back_button(b)
    return b.as_markup()


def kb_prune_keywords(*, keywords: list[str], excluded: set[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for idx, kw in enumerate((keywords or [])[:12]):
        key = " ".join(str(kw).split()).lower()
        mark = "❌" if key in excluded else "✅"
        b.row(InlineKeyboardButton(text=f"{mark} {kw}"[:64], callback_data=f"kw_toggle:{idx}"))
    b.row(
        InlineKeyboardButton(text="Добавить", callback_data="kw_add"),
        InlineKeyboardButton(text="Готово", callback_data="kw_done"),
    )
    b.row(InlineKeyboardButton(text="Включить все", callback_data="kw_all"))
    _add_back_button(b)
    return b.as_markup()


def kb_reports_per_day() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="Раз в день", callback_data="rpd_1"))
    b.add(InlineKeyboardButton(text="Два раза в день", callback_data="rpd_2"))
    _add_back_button(b)
    return b.as_markup()


def kb_timezone_method() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="Отправить геолокацию", callback_data="tz_location"))
    b.row(InlineKeyboardButton(text="Ввести таймзону вручную", callback_data="tz_manual"))
    b.row(InlineKeyboardButton(text="Оставить текущую", callback_data="tz_keep"))
    _add_back_button(b)
    return b.as_markup()


def kb_timezone_offsets(*, page: int, page_size: int = 9) -> InlineKeyboardMarkup:
    total = len(TZ_OFFSET_PRESETS)
    safe_page = max(0, page)
    start = safe_page * page_size
    end = min(total, start + page_size)

    b = InlineKeyboardBuilder()
    for offset in TZ_OFFSET_PRESETS[start:end]:
        b.add(InlineKeyboardButton(text=f"UTC{offset}", callback_data=f"tzpick:{offset}"))
    b.adjust(3, 3, 3)

    nav: list[InlineKeyboardButton] = []
    if safe_page > 0:
        nav.append(InlineKeyboardButton(text="<", callback_data=f"tzpick_page:{safe_page-1}"))
    nav.append(InlineKeyboardButton(text=f"{safe_page+1}/{(total + page_size - 1)//page_size}", callback_data="noop"))
    if end < total:
        nav.append(InlineKeyboardButton(text=">", callback_data=f"tzpick_page:{safe_page+1}"))
    b.row(*nav)
    _add_back_button(b)
    return b.as_markup()


def kb_competitors_next() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="Дальше", callback_data="comp_done"))
    _add_back_button(b)
    return b.as_markup()


def kb_link_candidates(*, candidates: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for idx, candidate in enumerate((candidates or [])[:5]):
        title = (candidate.get("title") or "").strip()
        handle = (candidate.get("handle") or "").strip()
        if not title:
            title = handle or (candidate.get("external_id") or "")
        text = title
        if handle:
            text = f"{title} (@{handle})"
        b.row(InlineKeyboardButton(text=text[:64], callback_data=f"link_pick:{idx}"))
    b.row(InlineKeyboardButton(text="Ввести вручную", callback_data="link_manual"))
    b.row(InlineKeyboardButton(text="Пропустить", callback_data="link_skip"))
    return b.as_markup()


def kb_youtube_suggested_competitors(*, report_id: int, suggestions: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for idx, suggestion in enumerate(list(suggestions or [])):
        channel_title = str(suggestion.get("channel_title") or suggestion.get("channel_id") or f"YouTube {idx + 1}").strip()
        b.row(
            InlineKeyboardButton(
                text=f"Добавить {idx + 1}. {channel_title}"[:64],
                callback_data=f"suggytadd:{report_id}:{idx}",
            )
        )
    return b.as_markup()


def kb_instagram_suggested_competitors(*, report_id: int, suggestions: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for idx, suggestion in enumerate(list(suggestions or [])):
        profile_name = str(
            suggestion.get("competitor_display_name")
            or suggestion.get("competitor_handle")
            or suggestion.get("competitor_external_id")
            or f"Instagram {idx + 1}"
        ).strip()
        b.row(
            InlineKeyboardButton(
                text=f"Добавить {idx + 1}. {profile_name}"[:64],
                callback_data=f"sugigadd:{report_id}:{idx}",
            )
        )
    return b.as_markup()


def kb_competitors_next_or_ignore() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="Дальше", callback_data="comp_done"))
    b.add(InlineKeyboardButton(text="Не учитывать список", callback_data="comp_clear"))
    b.adjust(1)
    _add_back_button(b)
    return b.as_markup()


def kb_time_presets_single() -> InlineKeyboardMarkup:
    """
    Presets for 1 report/day.
    """
    b = InlineKeyboardBuilder()
    for t in TIME_PRESETS:
        b.add(InlineKeyboardButton(text=t, callback_data=f"time1:{t}"))
    b.add(InlineKeyboardButton(text="Другое время", callback_data="time1:custom"))
    b.adjust(3, 3, 3, 3, 1)
    _add_back_button(b)
    return b.as_markup()


def kb_time_presets_first() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in TIME_PRESETS:
        b.add(InlineKeyboardButton(text=t, callback_data=f"time1pick:{t}"))
    b.add(InlineKeyboardButton(text="Ввести вручную", callback_data="time1pick:manual"))
    b.adjust(3, 3, 3, 3, 1)
    _add_back_button(b)
    return b.as_markup()


def kb_time_presets_second() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in TIME_PRESETS:
        b.add(InlineKeyboardButton(text=t, callback_data=f"time2:{t}"))
    b.add(InlineKeyboardButton(text="Ввести вручную", callback_data="time2:manual"))
    b.adjust(3, 3, 3, 3, 1)
    _add_back_button(b)
    return b.as_markup()


def _safe_http_url(raw_url: str | None) -> str | None:
    url = str(raw_url or "").strip()
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def kb_prune_competitors(
    *,
    competitor_rows: list[tuple[int, str, str | None]],
    excluded_ids: set[int],
    page: int,
    page_size: int,
) -> InlineKeyboardMarkup:
    total = len(competitor_rows)
    if total == 0:
        b = InlineKeyboardBuilder()
        b.add(InlineKeyboardButton(text="Продолжить", callback_data="prune_done"))
        _add_back_button(b)
        return b.as_markup()

    start = page * page_size
    end = min(total, start + page_size)
    b = InlineKeyboardBuilder()
    link_buttons: list[InlineKeyboardButton] = []

    for visible_idx, (cid, name, url) in enumerate(competitor_rows[start:end], start=1):
        mark = "❌" if cid in excluded_ids else "✅"
        buttons = [InlineKeyboardButton(text=f"{mark} {visible_idx}. {name}"[:64], callback_data=f"prune_toggle:{cid}")]
        safe_url = _safe_http_url(url)
        if safe_url:
            link_buttons.append(InlineKeyboardButton(text=f"{visible_idx}↗", url=safe_url))
        b.row(*buttons)

    for idx in range(0, len(link_buttons), 4):
        b.row(*link_buttons[idx : idx + 4])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="←", callback_data=f"prune_page:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{(total + page_size - 1)//page_size}", callback_data="noop"))
    if end < total:
        nav.append(InlineKeyboardButton(text="→", callback_data=f"prune_page:{page+1}"))
    b.row(*nav)

    b.row(
        InlineKeyboardButton(text="Включить всех", callback_data="prune_all"),
        InlineKeyboardButton(text="Готово", callback_data="prune_done"),
    )
    _add_back_button(b)
    return b.as_markup()


def kb_manage_competitors(
    *,
    competitor_rows: list[tuple[int, str, str | None]],
    selected_ids: set[int],
    page: int,
    page_size: int,
    toggle_prefix: str,
    page_prefix: str,
    all_callback: str,
    done_callback: str,
    done_text: str,
) -> InlineKeyboardMarkup:
    total = len(competitor_rows)
    b = InlineKeyboardBuilder()
    if total == 0:
        b.add(InlineKeyboardButton(text=done_text, callback_data=done_callback))
        _add_back_button(b)
        return b.as_markup()

    start = page * page_size
    end = min(total, start + page_size)
    link_buttons: list[InlineKeyboardButton] = []

    for visible_idx, (cid, name, url) in enumerate(competitor_rows[start:end], start=1):
        mark = "✅" if cid in selected_ids else "⬜"
        b.row(InlineKeyboardButton(text=f"{mark} {visible_idx}. {name}"[:64], callback_data=f"{toggle_prefix}:{cid}"))
        safe_url = _safe_http_url(url)
        if safe_url:
            link_buttons.append(InlineKeyboardButton(text=f"{visible_idx}↗", url=safe_url))

    for idx in range(0, len(link_buttons), 4):
        b.row(*link_buttons[idx : idx + 4])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="←", callback_data=f"{page_prefix}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{(total + page_size - 1)//page_size}", callback_data="noop"))
    if end < total:
        nav.append(InlineKeyboardButton(text="→", callback_data=f"{page_prefix}:{page+1}"))
    b.row(*nav)
    b.row(
        InlineKeyboardButton(text="Выбрать все", callback_data=all_callback),
        InlineKeyboardButton(text=done_text, callback_data=done_callback),
    )
    _add_back_button(b)
    return b.as_markup()


def kb_manage_stopwords(
    *,
    stopwords: list[str],
    selected_ids: set[int],
    page: int,
    page_size: int,
    toggle_prefix: str,
    page_prefix: str,
    all_callback: str,
    done_callback: str,
    done_text: str,
) -> InlineKeyboardMarkup:
    total = len(stopwords)
    b = InlineKeyboardBuilder()
    if total == 0:
        b.add(InlineKeyboardButton(text=done_text, callback_data=done_callback))
        _add_back_button(b)
        return b.as_markup()

    start = page * page_size
    end = min(total, start + page_size)
    for idx, word in enumerate(stopwords[start:end], start=start):
        mark = "✅" if idx in selected_ids else "⬜"
        b.row(InlineKeyboardButton(text=f"{mark} {word}"[:64], callback_data=f"{toggle_prefix}:{idx}"))

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="←", callback_data=f"{page_prefix}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{(total + page_size - 1)//page_size}", callback_data="noop"))
    if end < total:
        nav.append(InlineKeyboardButton(text="→", callback_data=f"{page_prefix}:{page+1}"))
    b.row(*nav)
    b.row(
        InlineKeyboardButton(text="Выбрать все", callback_data=all_callback),
        InlineKeyboardButton(text=done_text, callback_data=done_callback),
    )
    _add_back_button(b)
    return b.as_markup()
