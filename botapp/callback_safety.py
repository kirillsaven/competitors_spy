from __future__ import annotations

import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message


@dataclass(frozen=True)
class CallbackAck:
    callback_type: str
    user_id: int | None
    started_at: float
    status: str


@dataclass(frozen=True)
class MessageEdit:
    path: str
    elapsed_ms: float
    failure_reason: str | None = None


def callback_started() -> float:
    return perf_counter()


def exception_summary(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def is_message_not_modified(exc: Exception) -> bool:
    return isinstance(exc, TelegramBadRequest) and "message is not modified" in str(exc).lower()


async def safe_callback_ack(
    cb: CallbackQuery,
    *,
    callback_type: str,
    logger: logging.Logger,
    started_at: float | None = None,
    text: str | None = None,
    show_alert: bool = False,
) -> CallbackAck:
    started_at = started_at if started_at is not None else perf_counter()
    user_id = getattr(cb.from_user, "id", None)
    try:
        await cb.answer(text, show_alert=show_alert)
        return CallbackAck(
            callback_type=callback_type,
            user_id=user_id,
            started_at=started_at,
            status="success",
        )
    except Exception as exc:
        failure_reason = exception_summary(exc)
        logger.warning(
            "callback_ack_failed callback_type=%s user_id=%s callback_age_ms=%.1f failure_reason=%s",
            callback_type,
            user_id,
            (perf_counter() - started_at) * 1000,
            failure_reason,
        )
        return CallbackAck(
            callback_type=callback_type,
            user_id=user_id,
            started_at=started_at,
            status=f"failure:{type(exc).__name__}",
        )


async def safe_edit_message(
    message: Message,
    *,
    text: str,
    reply_markup,
    edit_mode: str = "text",
) -> MessageEdit:
    edit_started = perf_counter()
    if edit_mode == "markup":
        try:
            await message.edit_reply_markup(reply_markup=reply_markup)
            return MessageEdit("edit_reply_markup", (perf_counter() - edit_started) * 1000)
        except Exception as exc:
            if is_message_not_modified(exc):
                return MessageEdit("edit_reply_markup_noop", (perf_counter() - edit_started) * 1000)
            markup_error = exception_summary(exc)
            try:
                await message.edit_text(text, reply_markup=reply_markup)
                return MessageEdit("edit_reply_markup_fallback_edit_text", (perf_counter() - edit_started) * 1000)
            except Exception as fallback_exc:
                return MessageEdit(
                    "edit_reply_markup_failed",
                    (perf_counter() - edit_started) * 1000,
                    f"{markup_error}; fallback={exception_summary(fallback_exc)}",
                )

    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return MessageEdit("edit_text", (perf_counter() - edit_started) * 1000)
    except Exception as exc:
        text_error = exception_summary(exc)
        try:
            await message.edit_reply_markup(reply_markup=reply_markup)
            return MessageEdit("edit_text_fallback_edit_reply_markup", (perf_counter() - edit_started) * 1000)
        except Exception as fallback_exc:
            if is_message_not_modified(fallback_exc):
                return MessageEdit("edit_text_fallback_noop", (perf_counter() - edit_started) * 1000)
            return MessageEdit(
                "edit_text_failed",
                (perf_counter() - edit_started) * 1000,
                f"{text_error}; fallback={exception_summary(fallback_exc)}",
            )


def log_callback_observability(
    logger: logging.Logger,
    *,
    event_name: str,
    ack: CallbackAck,
    edit: MessageEdit | None = None,
    failure_reason: str | None = None,
    **fields: Any,
) -> None:
    edit_path = edit.path if edit else "none"
    edit_elapsed_ms = edit.elapsed_ms if edit else 0.0
    final_failure_reason = failure_reason or (edit.failure_reason if edit else None) or ""
    extra = " ".join(f"{key}={value}" for key, value in fields.items())
    if extra:
        extra = " " + extra
    logger.info(
        "%s callback_type=%s user_id=%s ack_status=%s edit_path=%s total_latency_ms=%.1f "
        "edit_elapsed_ms=%.1f failure_reason=%s%s",
        event_name,
        ack.callback_type,
        ack.user_id,
        ack.status,
        edit_path,
        (perf_counter() - ack.started_at) * 1000,
        edit_elapsed_ms,
        final_failure_reason,
        extra,
    )
