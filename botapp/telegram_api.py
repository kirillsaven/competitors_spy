from __future__ import annotations

import logging

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


class TelegramApiError(RuntimeError):
    pass


def send_message(*, chat_id: int, text: str, disable_preview: bool = True, reply_markup: dict | None = None) -> dict:
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "") or ""
    if not token:
        raise TelegramApiError("TELEGRAM_BOT_TOKEN is not set")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": disable_preview,
    }
    if isinstance(reply_markup, dict) and reply_markup:
        payload["reply_markup"] = reply_markup
    r = httpx.post(url, json=payload, timeout=15.0)
    try:
        data = r.json()
    except Exception:
        raise TelegramApiError(f"Telegram API invalid JSON: status={r.status_code}")
    if r.status_code >= 400 or not data.get("ok"):
        raise TelegramApiError(f"Telegram API error: status={r.status_code} body={data}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise TelegramApiError(f"Telegram API missing result payload: status={r.status_code} body={data}")
    return result

