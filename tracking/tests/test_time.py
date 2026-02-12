from __future__ import annotations

from datetime import UTC, datetime

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


def test_parse_iso8601_duration_seconds() -> None:
    assert parse_iso8601_duration_seconds("PT1H2M3S") == 3723
    assert parse_iso8601_duration_seconds("") is None

