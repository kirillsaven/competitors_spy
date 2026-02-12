from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


_HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_UTC_OFFSET_RE = re.compile(r"^\s*UTC\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\s*$", re.IGNORECASE)


class TimeParseError(ValueError):
    pass


class TimezoneParseError(ValueError):
    pass


def parse_hhmm(value: str) -> time:
    m = _HHMM_RE.match(value or "")
    if not m:
        raise TimeParseError("Ожидаю время в формате HH:MM (например, 09:00).")
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23) or not (0 <= mm <= 59):
        raise TimeParseError("Неверное время. Формат HH:MM (00:00..23:59).")
    return time(hour=hh, minute=mm)


def normalize_timezone_str(value: str) -> str:
    v = (value or "").strip()
    if not v:
        raise TimezoneParseError("Пустая таймзона.")

    m = _UTC_OFFSET_RE.match(v)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hh = int(m.group(2))
        mm = int(m.group(3) or "0")
        if hh > 23 or mm > 59:
            raise TimezoneParseError("Неверный UTC-оффсет.")
        total_minutes = sign * (hh * 60 + mm)
        sign_char = "+" if total_minutes >= 0 else "-"
        total_minutes_abs = abs(total_minutes)
        hh_abs = total_minutes_abs // 60
        mm_abs = total_minutes_abs % 60
        return f"UTC{sign_char}{hh_abs:02d}:{mm_abs:02d}"

    # Assume IANA tz name.
    try:
        ZoneInfo(v)
    except Exception as e:
        raise TimezoneParseError("Неверная таймзона. Используй IANA (например: Europe/Moscow) или UTC+03:00.") from e
    return v


def tzinfo_from_timezone_str(timezone_str: str):
    tzs = normalize_timezone_str(timezone_str)
    m = re.match(r"^UTC([+-])(\d{2}):(\d{2})$", tzs)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hh = int(m.group(2))
        mm = int(m.group(3))
        return timezone(timedelta(minutes=sign * (hh * 60 + mm)))
    return ZoneInfo(tzs)


def compute_next_run_at(timezone_str: str, times: list[str], now_utc: datetime) -> datetime:
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")

    if not times:
        times = ["09:00"]

    tz = tzinfo_from_timezone_str(timezone_str)
    now_local = now_utc.astimezone(tz)
    local_date = now_local.date()

    candidates: list[datetime] = []
    for t_str in times:
        t = parse_hhmm(t_str)
        dt_local = datetime(
            year=local_date.year,
            month=local_date.month,
            day=local_date.day,
            hour=t.hour,
            minute=t.minute,
            tzinfo=tz,
        )
        if dt_local <= now_local:
            dt_local = dt_local + timedelta(days=1)
        candidates.append(dt_local)

    next_local = min(candidates)
    return next_local.astimezone(UTC)


def format_dt_local(dt_utc: datetime, timezone_str: str) -> str:
    tz = tzinfo_from_timezone_str(timezone_str)
    return dt_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M")


_ISO8601_DURATION_RE = re.compile(r"^P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def parse_iso8601_duration_seconds(value: str) -> int | None:
    if not value:
        return None
    m = _ISO8601_DURATION_RE.match(value)
    if not m:
        return None
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    seconds = int(m.group(4) or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds
