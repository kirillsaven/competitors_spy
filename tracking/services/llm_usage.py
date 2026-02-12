from __future__ import annotations

from datetime import datetime

from django.conf import settings

from tracking.models import TgUser


def try_consume_llm_call(*, user: TgUser, now_utc: datetime) -> bool:
    """
    Simple per-user per-day limiter for internal LLM usage.

    This is NOT exposed as a chat feature: only used for internal helpers like niche inference.
    """
    if not getattr(settings, "GOOGLE_LLM_API_KEY", ""):
        return False

    max_calls = int(getattr(settings, "GOOGLE_LLM_MAX_CALLS_PER_USER_PER_DAY", 3))
    if max_calls <= 0:
        return False

    today = now_utc.date().isoformat()
    limits = dict(user.limits_json or {})
    llm = dict(limits.get("llm") or {})

    day = str(llm.get("day") or "")
    count_raw = llm.get("count")
    try:
        count = int(count_raw or 0)
    except Exception:
        count = 0

    if day != today:
        day = today
        count = 0

    if count >= max_calls:
        return False

    llm["day"] = day
    llm["count"] = count + 1
    limits["llm"] = llm
    user.limits_json = limits
    user.save(update_fields=["limits_json", "updated_at"])
    return True

