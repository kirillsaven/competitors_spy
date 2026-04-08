from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any

from aiogram.types import CallbackQuery

from botapp.callback_safety import callback_started, safe_callback_ack
from botapp.picker_session import (
    PickerSessionKeys,
    picker_select_all,
    picker_set_page,
    picker_toggle_selection,
)

PickerRender = Callable[..., Awaitable[None]]
PickerCountResolver = Callable[[MutableMapping[str, Any]], int]
PickerSelectAllResolver = Callable[[MutableMapping[str, Any]], Iterable[int]]


async def _run_picker_controller(
    *,
    cb: CallbackQuery,
    state,
    logger: logging.Logger,
    callback_type: str,
    mutate_session: Callable[[MutableMapping[str, Any]], Awaitable[int]],
    render: PickerRender,
) -> None:
    started_at = callback_started()
    ack = await safe_callback_ack(cb, callback_type=callback_type, logger=logger, started_at=started_at)
    if not cb.message:
        return
    data = await state.get_data()
    page_before = await mutate_session(data)
    await render(
        cb.message,
        state,
        data=data,
        edit_mode="markup",
        callback_info={"ack": ack, "page_before": page_before},
    )


async def handle_picker_page_callback(
    *,
    cb: CallbackQuery,
    state,
    logger: logging.Logger,
    callback_type: str,
    session_keys: PickerSessionKeys,
    requested_page: int,
    total_count: PickerCountResolver,
    page_size: int,
    render: PickerRender,
) -> None:
    async def mutate_session(data: MutableMapping[str, Any]) -> int:
        page_before, _ = await picker_set_page(
            state,
            data,
            keys=session_keys,
            requested_page=requested_page,
            total=total_count(data),
            page_size=page_size,
        )
        return page_before

    await _run_picker_controller(
        cb=cb,
        state=state,
        logger=logger,
        callback_type=callback_type,
        mutate_session=mutate_session,
        render=render,
    )


async def handle_picker_toggle_callback(
    *,
    cb: CallbackQuery,
    state,
    logger: logging.Logger,
    callback_type: str,
    session_keys: PickerSessionKeys,
    item_id: int,
    render: PickerRender,
) -> None:
    async def mutate_session(data: MutableMapping[str, Any]) -> int:
        page_before, _ = await picker_toggle_selection(
            state,
            data,
            keys=session_keys,
            item_id=item_id,
        )
        return page_before

    await _run_picker_controller(
        cb=cb,
        state=state,
        logger=logger,
        callback_type=callback_type,
        mutate_session=mutate_session,
        render=render,
    )


async def handle_picker_select_all_callback(
    *,
    cb: CallbackQuery,
    state,
    logger: logging.Logger,
    callback_type: str,
    session_keys: PickerSessionKeys,
    selected_ids: PickerSelectAllResolver,
    render: PickerRender,
) -> None:
    async def mutate_session(data: MutableMapping[str, Any]) -> int:
        page_before, _ = await picker_select_all(
            state,
            data,
            keys=session_keys,
            selected_ids=selected_ids(data),
        )
        return page_before

    await _run_picker_controller(
        cb=cb,
        state=state,
        logger=logger,
        callback_type=callback_type,
        mutate_session=mutate_session,
        render=render,
    )
