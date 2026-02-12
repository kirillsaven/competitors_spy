from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


def kb_keywords_confirm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="ОК", callback_data="kw_ok"))
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
    b.add(KeyboardButton(text="Оставить по умолчанию"))
    b.adjust(1)
    return b.as_markup(resize_keyboard=True, one_time_keyboard=True)

