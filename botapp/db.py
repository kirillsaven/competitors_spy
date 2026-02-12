from __future__ import annotations

from typing import Callable, TypeVar

from asgiref.sync import sync_to_async

T = TypeVar("T")


async def db_run(fn: Callable[[], T]) -> T:
    return await sync_to_async(fn, thread_sensitive=True)()


async def db_call(fn: Callable[..., T], *args, **kwargs) -> T:
    return await sync_to_async(fn, thread_sensitive=True)(*args, **kwargs)

