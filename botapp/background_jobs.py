from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

_RUNNING_JOBS: dict[tuple[str, int], asyncio.Task[None]] = {}


def schedule_user_background_job(
    *,
    kind: str,
    user_id: int,
    logger: logging.Logger,
    job_factory: Callable[[], Awaitable[None]],
) -> bool:
    key = (str(kind or "").strip(), int(user_id))
    running = _RUNNING_JOBS.get(key)
    if running is not None and not running.done():
        logger.info("background_job_schedule kind=%s user_id=%s status=already_running", key[0], key[1])
        return False

    async def _runner() -> None:
        try:
            await job_factory()
        except Exception:
            logger.exception("background_job_failed kind=%s user_id=%s", key[0], key[1])

    task = asyncio.create_task(_runner(), name=f"{key[0]}:{key[1]}")
    _RUNNING_JOBS[key] = task

    def _cleanup(done_task: asyncio.Task[None]) -> None:
        if _RUNNING_JOBS.get(key) is done_task:
            _RUNNING_JOBS.pop(key, None)
        try:
            done_task.result()
        except Exception:
            pass

    task.add_done_callback(_cleanup)
    logger.info("background_job_schedule kind=%s user_id=%s status=scheduled", key[0], key[1])
    return True
