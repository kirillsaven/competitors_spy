from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


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

def kb_seed_candidates(*, candidates: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for idx, c in enumerate((candidates or [])[:8]):
        title = (c.get("title") or "").strip()
        handle = (c.get("handle") or "").strip()
        if not title:
            title = handle or (c.get("external_id") or "")
        txt = title
        if handle:
            txt = f"{title} (@{handle})"
        b.row(InlineKeyboardButton(text=txt[:64], callback_data=f"seed_pick:{idx}"))
    b.row(InlineKeyboardButton(text="Это не то", callback_data="seed_retry"))
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
    return b.as_markup()


def kb_reports_per_day() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="Раз в день", callback_data="rpd_1"))
    b.add(InlineKeyboardButton(text="Два раза в день", callback_data="rpd_2"))
    return b.as_markup()


def kb_timezone_method() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Отправить геолокацию", request_location=True))
    b.add(KeyboardButton(text="Ввести таймзону вручную"))
    b.add(KeyboardButton(text="Оставить текущую"))
    b.adjust(1)
    return b.as_markup(resize_keyboard=True, one_time_keyboard=True)

def kb_competitors_next() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="Дальше", callback_data="comp_done"))
    return b.as_markup()


def kb_competitors_next_or_ignore() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="Дальше", callback_data="comp_done"))
    b.add(InlineKeyboardButton(text="Не учитывать список", callback_data="comp_clear"))
    b.adjust(1)
    return b.as_markup()


def kb_report_now() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="Отчет сейчас", callback_data="report_now"))
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
    return b.as_markup()


def kb_time_presets_first() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in TIME_PRESETS:
        b.add(InlineKeyboardButton(text=t, callback_data=f"time1pick:{t}"))
    b.add(InlineKeyboardButton(text="Ввести вручную", callback_data="time1pick:manual"))
    b.adjust(3, 3, 3, 3, 1)
    return b.as_markup()


def kb_time_presets_second() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in TIME_PRESETS:
        b.add(InlineKeyboardButton(text=t, callback_data=f"time2:{t}"))
    b.add(InlineKeyboardButton(text="Ввести вручную", callback_data="time2:manual"))
    b.adjust(3, 3, 3, 3, 1)
    return b.as_markup()


def kb_prune_competitors(*, competitor_rows: list[tuple[int, str]], excluded_ids: set[int], page: int, page_size: int) -> InlineKeyboardMarkup:
    total = len(competitor_rows)
    if total == 0:
        b = InlineKeyboardBuilder()
        b.add(InlineKeyboardButton(text="Продолжить", callback_data="prune_done"))
        return b.as_markup()

    start = page * page_size
    end = min(total, start + page_size)
    b = InlineKeyboardBuilder()

    for cid, name in competitor_rows[start:end]:
        mark = "❌" if cid in excluded_ids else "✅"
        # One channel per row: easier to tap, avoids Telegram row limits.
        b.row(InlineKeyboardButton(text=f"{mark} {name}", callback_data=f"prune_toggle:{cid}"))

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
    return b.as_markup()
