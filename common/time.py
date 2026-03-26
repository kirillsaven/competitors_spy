from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


_HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_UTC_OFFSET_RE = re.compile(r"^\s*UTC\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\s*$", re.IGNORECASE)
_UTC_NORMALIZED_RE = re.compile(r"^UTC([+-])(\d{2}):(\d{2})$")

# For users who keep a fixed UTC offset instead of an IANA tz, show a friendly city hint.
# This is intentionally conservative: we only map the most common RU offsets.
_RU_OFFSET_CITY: dict[str, str] = {
    "UTC+02:00": "Калининград",
    "UTC+03:00": "Москва",
    "UTC+04:00": "Самара",
    "UTC+05:00": "Екатеринбург",
    "UTC+06:00": "Омск",
    "UTC+07:00": "Красноярск",
    "UTC+08:00": "Иркутск",
    "UTC+09:00": "Якутск",
    "UTC+10:00": "Владивосток",
    "UTC+11:00": "Магадан",
    "UTC+12:00": "Анадырь",
}


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


def compute_next_run_at(
    timezone_str: str,
    times: list[str],
    now_utc: datetime,
    *,
    min_delay: timedelta | None = None,
) -> datetime:
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")

    if not times:
        times = ["09:00"]

    tz = tzinfo_from_timezone_str(timezone_str)
    effective_now_utc = now_utc + (min_delay or timedelta())
    now_local = effective_now_utc.astimezone(tz)
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


def format_timezone_label(timezone_str: str, *, now_utc: datetime | None = None) -> str:
    """
    Human-friendly timezone label.

    Examples:
    - "Asia/Krasnoyarsk (UTC+07:00)"
    - "UTC+07:00 (Красноярск)"
    """
    tzs = normalize_timezone_str(timezone_str)
    if _UTC_NORMALIZED_RE.match(tzs):
        city = _RU_OFFSET_CITY.get(tzs)
        return f"{tzs} ({city})" if city else tzs

    try:
        tz = ZoneInfo(tzs)
    except Exception:
        return tzs

    now = now_utc if now_utc is not None else datetime.now(UTC)
    try:
        offset = now.astimezone(tz).utcoffset()
    except Exception:
        offset = None
    if offset is None:
        return tzs

    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes_abs = abs(total_minutes)
    hh = total_minutes_abs // 60
    mm = total_minutes_abs % 60
    return f"{tzs} (UTC{sign}{hh:02d}:{mm:02d})"


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
