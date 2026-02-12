from __future__ import annotations

import math


def quantile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("values is empty")
    if not (0.0 <= q <= 1.0):
        raise ValueError("q must be in [0, 1]")
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(xs[lo])
    w = pos - lo
    return float(xs[lo] * (1.0 - w) + xs[hi] * w)


def median(values: list[float]) -> float:
    return quantile(values, 0.5)


def iqr(values: list[float]) -> float:
    return quantile(values, 0.75) - quantile(values, 0.25)

