from __future__ import annotations

import math
from typing import Any

import pandas as pd

from data_utils import interval_to_timedelta


def price_precision(frame: pd.DataFrame) -> tuple[int, float]:
    """Choose display precision without changing the underlying prices."""
    precision = 0
    for value in frame[["open", "high", "low", "close"]].to_numpy().ravel():
        number = float(value)
        if not math.isfinite(number):
            continue
        rendered = f"{number:.8f}".rstrip("0").rstrip(".")
        decimals = len(rendered.partition(".")[2])
        precision = max(precision, decimals)
    precision = min(max(precision, 2), 8)
    return precision, 10 ** (-precision)


def epoch_seconds(series: pd.Series) -> list[int]:
    """Convert timezone-aware timestamps to UTC epoch seconds for the JS chart."""
    return [int(pd.Timestamp(value).timestamp()) for value in series]


def with_whitespace(
    rows: list[dict[str, Any]],
    *,
    times: list[int],
    interval: str,
    max_points: int = 250_000,
) -> list[dict[str, Any]]:
    """Insert display-only whitespace points so real time gaps remain visible.

    Whitespace points have no OHLC/volume values and therefore are not candles.
    """
    expected = interval_to_timedelta(interval)
    if expected is None or len(rows) < 2:
        return rows
    step = int(expected.total_seconds())
    if step <= 0:
        return rows

    start, end = min(times), max(times)
    required_points = ((end - start) // step) + 1
    if required_points > max_points:
        raise ValueError(
            f"Uncompressed gap view would require {required_points:,} chart points. "
            "Turn on 'Compress non-trading gaps' for this range."
        )

    by_time = {int(row["time"]): row for row in rows}
    expanded: list[dict[str, Any]] = []
    current = start
    while current <= end:
        expanded.append(by_time.get(current, {"time": current}))
        current += step
    return expanded
