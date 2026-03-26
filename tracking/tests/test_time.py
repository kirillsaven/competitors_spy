from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from common.time import compute_next_run_at, normalize_timezone_str, parse_hhmm, parse_iso8601_duration_seconds


def test_parse_hhmm() -> None:
    t = parse_hhmm("09:00")
    assert (t.hour, t.minute) == (9, 0)


def test_normalize_timezone_str() -> None:
    assert normalize_timezone_str("UTC+3") == "UTC+03:00"
    assert normalize_timezone_str("UTC-03:30") == "UTC-03:30"


def test_compute_next_run_at_utc_offset() -> None:
    now = datetime(2026, 2, 12, 6, 0, tzinfo=UTC)  # 09:00 local at UTC+03:00
    nxt = compute_next_run_at("UTC+03:00", ["09:00", "21:00"], now)
    # Since dt_local at 09:00 is <= now_local, next should be 21:00 local => 18:00 UTC.
    assert nxt == datetime(2026, 2, 12, 18, 0, tzinfo=UTC)


def test_compute_next_run_at_preserves_requested_wall_clock_time() -> None:
    now = datetime(2026, 3, 26, 11, 30, tzinfo=UTC)  # 18:30 in Asia/Bangkok
    nxt = compute_next_run_at("Asia/Bangkok", ["19:45"], now)

    assert nxt == datetime(2026, 3, 26, 12, 45, tzinfo=UTC)
    assert nxt.astimezone(ZoneInfo("Asia/Bangkok")).strftime("%H:%M") == "19:45"


def test_compute_next_run_at_honors_iana_timezone_offset() -> None:
    now = datetime(2026, 6, 1, 16, 50, tzinfo=UTC)  # 18:50 in Europe/Berlin during DST
    nxt = compute_next_run_at("Europe/Berlin", ["19:45"], now)

    assert nxt == datetime(2026, 6, 1, 17, 45, tzinfo=UTC)
    assert nxt.astimezone(ZoneInfo("Europe/Berlin")).strftime("%H:%M") == "19:45"


def test_parse_iso8601_duration_seconds() -> None:
    assert parse_iso8601_duration_seconds("PT1H2M3S") == 3723
    assert parse_iso8601_duration_seconds("") is None

