from __future__ import annotations

import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Awaitable, Callable, Generic, TypeVar

from aiogram.types import InlineKeyboardMarkup, Message

T = TypeVar("T")


@dataclass(frozen=True)
class PreparedPickerOpen:
    text: str
    reply_markup: InlineKeyboardMarkup | None
    row_count: int


@dataclass(frozen=True)
class PickerOpenOutcome(Generic[T]):
    load_result: T | None
    open_path: str
    status: str
    row_count: int
    total_open_ms: float


def _log_picker_open(
    logger: logging.Logger,
    *,
    event_name: str,
    user_id: int,
    open_path: str,
    total_open_ms: float,
    status: str,
    failure_reason: str | None = None,
    **fields: Any,
) -> None:
    extra = " ".join(f"{key}={value}" for key, value in fields.items())
    if extra:
        extra = " " + extra
    if failure_reason is None:
        logger.info(
            "%s user_id=%s open_path=%s%s total_open_ms=%.1f status=%s",
            event_name,
            user_id,
            open_path,
            extra,
            total_open_ms,
            status,
        )
        return
    logger.info(
        "%s user_id=%s open_path=%s%s total_open_ms=%.1f status=%s failure_reason=%s",
        event_name,
        user_id,
        open_path,
        extra,
        total_open_ms,
        status,
        failure_reason,
    )


async def open_picker_with_cache(
    *,
    message: Message,
    logger: logging.Logger,
    event_name: str,
    user_id: int,
    peek: Callable[[], Awaitable[T | None]],
    load: Callable[[], Awaitable[T]],
    loading_text: str,
    build_failure_text: Callable[[Exception], str],
    build_empty_text: Callable[[T], str],
    prepare_picker: Callable[[T], Awaitable[PreparedPickerOpen | None]],
    clear_state: Callable[[], Awaitable[None]],
    build_log_fields: Callable[[T | None, int], dict[str, Any]],
) -> PickerOpenOutcome[T]:
    opened_at = perf_counter()
    load_result = await peek()
    open_path = "single_send" if load_result is not None else "placeholder_edit"
    placeholder_message = None
    try:
        if load_result is None:
            placeholder_message = await message.answer(loading_text)
            load_result = await load()
    except Exception as exc:
        await clear_state()
        total_open_ms = (perf_counter() - opened_at) * 1000
        _log_picker_open(
            logger,
            event_name=event_name,
            user_id=user_id,
            open_path=open_path,
            total_open_ms=total_open_ms,
            status="failure",
            failure_reason=str(exc),
            **build_log_fields(None, 0),
        )
        failure_text = build_failure_text(exc)
        if placeholder_message is not None:
            await placeholder_message.edit_text(failure_text)
        else:
            await message.answer(failure_text)
        return PickerOpenOutcome(
            load_result=None,
            open_path=open_path,
            status="failure",
            row_count=0,
            total_open_ms=total_open_ms,
        )

    prepared = await prepare_picker(load_result)
    if prepared is None:
        await clear_state()
        total_open_ms = (perf_counter() - opened_at) * 1000
        _log_picker_open(
            logger,
            event_name=event_name,
            user_id=user_id,
            open_path=open_path,
            total_open_ms=total_open_ms,
            status="empty",
            **build_log_fields(load_result, 0),
        )
        empty_text = build_empty_text(load_result)
        if placeholder_message is not None:
            await placeholder_message.edit_text(empty_text)
        else:
            await message.answer(empty_text)
        return PickerOpenOutcome(
            load_result=load_result,
            open_path=open_path,
            status="empty",
            row_count=0,
            total_open_ms=total_open_ms,
        )

    if open_path == "single_send":
        await message.answer(prepared.text, reply_markup=prepared.reply_markup)
    else:
        await placeholder_message.edit_text(prepared.text, reply_markup=prepared.reply_markup)

    total_open_ms = (perf_counter() - opened_at) * 1000
    _log_picker_open(
        logger,
        event_name=event_name,
        user_id=user_id,
        open_path=open_path,
        total_open_ms=total_open_ms,
        status="success",
        **build_log_fields(load_result, prepared.row_count),
    )
    return PickerOpenOutcome(
        load_result=load_result,
        open_path=open_path,
        status="success",
        row_count=prepared.row_count,
        total_open_ms=total_open_ms,
    )
