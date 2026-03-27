from __future__ import annotations

from tracking.models import TgUser


def normalize_tg_username(value: str | None) -> str:
    username = (value or "").strip()
    if username.startswith("@"):
        username = username[1:]
    return username


def normalize_profile_text(value: str | None) -> str:
    return (value or "").strip()


def upsert_tg_user(
    *,
    telegram_user_id: int,
    chat_id: int,
    username: str | None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
) -> tuple[TgUser, bool]:
    return TgUser.objects.update_or_create(
        tg_user_id=telegram_user_id,
        defaults={
            "tg_chat_id": chat_id,
            "tg_username": normalize_tg_username(username),
            "tg_first_name": normalize_profile_text(first_name),
            "tg_last_name": normalize_profile_text(last_name),
            "tg_language_code": normalize_profile_text(language_code),
        },
    )
