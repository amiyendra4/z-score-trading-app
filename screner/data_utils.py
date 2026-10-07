from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

import pandas as pd


REQUIRED_COLUMNS = {"time", "open", "high", "low", "close"}
_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)\s*$", re.IGNORECASE)


def interval_to_timedelta(interval: str) -> pd.Timedelta | None:
    """Convert common QH interval values such as 1M, 15M, 1H, 4H, 1D to Timedelta."""
    match = _INTERVAL_RE.fullmatch(str(interval))
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2).lower()
    if value <= 0:
        return None
    if unit in {"s", "sec", "secs", "second", "seconds"}:
        return pd.to_timedelta(value, unit="s")
    if unit in {"m", "min", "mins", "minute", "minutes"}:
        return pd.to_timedelta(value, unit="min")
    if unit in {"h", "hr", "hrs", "hour", "hours"}:
        return pd.to_timedelta(value, unit="h")
    if unit in {"d", "day", "days"}:
        return pd.to_timedelta(value, unit="D")
    if unit in {"w", "week", "weeks"}:
        return pd.to_timedelta(value * 7, unit="D")
    return None


class DataValidationError(ValueError):
    """Raised when a QH response cannot safely be interpreted as OHLC data."""


@dataclass(frozen=True)
class GapSummary:
    gap_count: int
    missing_intervals: int


def _looks_like_bar(value: Any) -> bool:
    return isinstance(value, dict) and REQUIRED_COLUMNS.issubset(value)


def _find_bar_records(value: Any) -> list[dict[str, Any]]:
    """Find the first OHLC record list in common QH response envelopes."""
    if isinstance(value, list):
        bars = [item for item in value if _looks_like_bar(item)]
        if bars:
            return bars
        for item in value:
            nested = _find_bar_records(item)
            if nested:
                return nested
    elif isinstance(value, dict):
        for key in ("data", "results", "items", "ohlc", "candles", "bars"):
            if key in value:
                nested = _find_bar_records(value[key])
                if nested:
                    return nested
        for nested_value in value.values():
            nested = _find_bar_records(nested_value)
            if nested:
                return nested
    return []


def _first_present(columns: Iterable[str], candidates: Iterable[str]) -> str | None:
    available = set(columns)
    return next((name for name in candidates if name in available), None)


def prepare_ohlc_data(
    payload: object,
    timestamp_offset_minutes: int = 65,
    instrument: str | None = None,
) -> pd.DataFrame:
    """Normalize QH bars without resampling, filling, or synthesizing observations."""
    records = _find_bar_records(payload)
    if not records:
        raise DataValidationError(
            "The JSON does not contain recognizable QH OHLC bars with time/open/high/low/close."
        )

    frame = pd.DataFrame.from_records(records).copy()
    product_column = _first_present(frame.columns, ("product", "instrument", "symbol"))
    if product_column is None:
        frame["product"] = instrument or "Unknown"
    elif product_column != "product":
        frame["product"] = frame[product_column]

    if instrument and "product" in frame:
        exact = frame["product"].astype(str).str.casefold() == instrument.casefold()
        if exact.any():
            frame = frame.loc[exact].copy()

    for column in ("time", "open", "high", "low", "close", "volume"):
        if column not in frame:
            if column == "volume":
                frame[column] = 0
            else:
                raise DataValidationError(f"Required OHLC field is missing: {column}")
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    invalid_numeric = frame[["time", "open", "high", "low", "close", "volume"]].isna().any(axis=1)
    if invalid_numeric.any():
        raise DataValidationError(
            f"{int(invalid_numeric.sum())} bar(s) contain missing or non-numeric OHLC values."
        )
    if (frame["volume"] < 0).any():
        raise DataValidationError("Volume cannot be negative.")

    invalid_ohlc = (
        (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
        | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
    )
    if invalid_ohlc.any():
        raise DataValidationError(
            f"{int(invalid_ohlc.sum())} bar(s) violate OHLC high/low bounds."
        )

    frame["raw_timestamp_ms"] = frame["time"].astype("int64")
    frame["raw_time_utc"] = pd.to_datetime(
        frame["raw_timestamp_ms"], unit="ms", utc=True, errors="raise"
    )
    frame["raw_time_london"] = frame["raw_time_utc"].dt.tz_convert("Europe/London")
    frame["strategy_time_london"] = frame["raw_time_london"] - pd.to_timedelta(
        timestamp_offset_minutes, unit="min"
    )

    # Keep one copy of an identical API bar; do not aggregate duplicate timestamps.
    frame = frame.drop_duplicates(
        subset=["product", "raw_timestamp_ms", "open", "high", "low", "close", "volume"]
    )
    frame = frame.sort_values("strategy_time_london", kind="stable").reset_index(drop=True)
    return frame[
        [
            "product",
            "raw_timestamp_ms",
            "raw_time_utc",
            "raw_time_london",
            "strategy_time_london",
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]
    ]


def resample_ohlc_data(frame: pd.DataFrame, target_interval: str) -> pd.DataFrame:
    """Aggregate real source bars into a larger strategy interval without filling empty buckets.

    Every output candle contains at least one observed source candle. Missing source intervals
    remain missing; no price/volume is forward-filled.
    """
    target = interval_to_timedelta(target_interval)
    if target is None or target <= pd.Timedelta(0):
        raise DataValidationError(f"Unsupported strategy interval for local aggregation: {target_interval}")
    if frame.empty:
        return frame.copy()

    required = {"product", "strategy_time_london", "open", "high", "low", "close", "volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise DataValidationError(
            "Cannot aggregate OHLC because required columns are missing: " + ", ".join(sorted(missing))
        )

    data = frame.copy().sort_values("strategy_time_london", kind="stable")
    # Floor in UTC to avoid ambiguous/non-existent local timestamps around DST transitions.
    utc_times = data["strategy_time_london"].dt.tz_convert("UTC")
    freq = pd.tseries.frequencies.to_offset(target)
    data["_bucket_utc"] = utc_times.dt.floor(freq)

    grouped = data.groupby(["product", "_bucket_utc"], sort=True, observed=True)
    out = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        source_bar_count=("close", "size"),
    ).reset_index()

    out["strategy_time_london"] = out["_bucket_utc"].dt.tz_convert("Europe/London")
    # Reconstruct the corresponding raw/QH timestamp using the observed offset between raw and strategy time.
    offset = (
        data["raw_time_utc"].iloc[0]
        - data["strategy_time_london"].iloc[0].tz_convert("UTC")
        if "raw_time_utc" in data.columns
        else pd.Timedelta(0)
    )
    out["raw_time_utc"] = out["_bucket_utc"] + offset
    out["raw_time_london"] = out["raw_time_utc"].dt.tz_convert("Europe/London")
    out["raw_timestamp_ms"] = (out["raw_time_utc"].astype("int64") // 1_000_000).astype("int64")
    out = out.drop(columns=["_bucket_utc"])

    return out[
        [
            "product",
            "raw_timestamp_ms",
            "raw_time_utc",
            "raw_time_london",
            "strategy_time_london",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "source_bar_count",
        ]
    ].sort_values("strategy_time_london", kind="stable").reset_index(drop=True)


def summarize_gaps(frame: pd.DataFrame, interval: str) -> GapSummary:
    expected = interval_to_timedelta(interval)
    if expected is None or len(frame) < 2:
        return GapSummary(0, 0)
    deltas = frame["strategy_time_london"].sort_values().diff().dropna()
    gap_deltas = deltas[deltas > expected]
    missing = sum(max(0, int(delta / expected) - 1) for delta in gap_deltas)
    return GapSummary(gap_count=len(gap_deltas), missing_intervals=missing)
