from __future__ import annotations

import asyncio
import logging
import os

import django
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from django.conf import settings


def _setup_django() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()


async def main() -> None:
    _setup_django()

    # Import handlers only after django.setup(), otherwise models import will crash.
    from botapp.handlers.common import router as common_router
    from botapp.handlers.setup import router as setup_router
    from botapp.handlers.status import router as status_router

    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "") or ""
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    bot = Bot(token=token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(common_router)
    dp.include_router(setup_router)
    dp.include_router(status_router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    asyncio.run(main())
