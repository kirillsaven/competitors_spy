from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.conf import settings

from tracking.models import TgUser


@dataclass(frozen=True)
class LlmDecision:
    allow: bool
    reason: str  # ok | not_configured | disabled | limit_reached
    used_today: int
    max_calls_per_day: int


def decide_and_consume_llm_call(*, user: TgUser, now_utc: datetime) -> LlmDecision:
    """
    Per-user per-day limiter for internal LLM usage.

    This is NOT exposed as a chat feature: only used for internal helpers like niche inference.
    """
    if not getattr(settings, "GOOGLE_LLM_API_KEY", ""):
        return LlmDecision(allow=False, reason="not_configured", used_today=0, max_calls_per_day=0)

    max_calls = int(getattr(settings, "GOOGLE_LLM_MAX_CALLS_PER_USER_PER_DAY", 10))
    if max_calls <= 0:
        return LlmDecision(allow=False, reason="disabled", used_today=0, max_calls_per_day=max_calls)

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
        return LlmDecision(allow=False, reason="limit_reached", used_today=count, max_calls_per_day=max_calls)

    llm["day"] = day
    llm["count"] = count + 1
    limits["llm"] = llm
    user.limits_json = limits
    user.save(update_fields=["limits_json", "updated_at"])
    return LlmDecision(allow=True, reason="ok", used_today=count + 1, max_calls_per_day=max_calls)

