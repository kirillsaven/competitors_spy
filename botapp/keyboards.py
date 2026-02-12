from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


def kb_keywords_confirm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="Подходит", callback_data="kw_ok"))
    b.add(InlineKeyboardButton(text="Изменить", callback_data="kw_edit"))
    return b.as_markup()


def kb_reports_per_day() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="1 раз в день", callback_data="rpd_1"))
    b.add(InlineKeyboardButton(text="2 раза в день", callback_data="rpd_2"))
    return b.as_markup()


def kb_timezone_method() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Отправить геолокацию", request_location=True))
    b.add(KeyboardButton(text="Ввести таймзону вручную"))
    b.add(KeyboardButton(text="Оставить текущую"))
    b.adjust(1)
    return b.as_markup(resize_keyboard=True, one_time_keyboard=True)


def kb_skip_competitors() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="Пропустить", callback_data="comp_skip"))
    return b.as_markup()


def kb_competitors_optional() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="Пропустить", callback_data="comp_skip"))
    b.add(InlineKeyboardButton(text="Дальше", callback_data="comp_done"))
    b.adjust(2)
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
    for t in ["09:00", "10:00", "12:00", "18:00", "21:00"]:
        b.add(InlineKeyboardButton(text=t, callback_data=f"time1:{t}"))
    b.add(InlineKeyboardButton(text="Другое время", callback_data="time1:custom"))
    b.adjust(3, 2, 1)
    return b.as_markup()


def kb_time_presets_pair() -> InlineKeyboardMarkup:
    """
    Presets for 2 reports/day.
    """
    b = InlineKeyboardBuilder()
    for a, c in [("09:00", "21:00"), ("10:00", "20:00"), ("12:00", "18:00")]:
        b.add(InlineKeyboardButton(text=f"{a} + {c}", callback_data=f"timep:{a},{c}"))
    b.add(InlineKeyboardButton(text="Другое время", callback_data="timep:custom"))
    b.adjust(1, 1, 1, 1)
    return b.as_markup()


def kb_time_presets_first() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in ["09:00", "10:00", "12:00", "18:00", "21:00"]:
        b.add(InlineKeyboardButton(text=t, callback_data=f"time1pick:{t}"))
    b.add(InlineKeyboardButton(text="Ввести вручную", callback_data="time1pick:manual"))
    b.adjust(3, 2, 1)
    return b.as_markup()


def kb_time_presets_second() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in ["09:00", "10:00", "12:00", "18:00", "21:00"]:
        b.add(InlineKeyboardButton(text=t, callback_data=f"time2:{t}"))
    b.add(InlineKeyboardButton(text="Ввести вручную", callback_data="time2:manual"))
    b.adjust(3, 2, 1)
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
