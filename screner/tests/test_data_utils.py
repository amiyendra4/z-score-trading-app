import json
from pathlib import Path

import pandas as pd

from data_utils import interval_to_timedelta, prepare_ohlc_data, summarize_gaps


PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_sample_preserves_rows_and_raw_timestamps():
    payload = json.loads(
        (PROJECT_DIR / "sample_data" / "response_1788346938262.json").read_text(
            encoding="utf-8"
        )
    )
    frame = prepare_ohlc_data(payload, timestamp_offset_minutes=65)

    assert len(frame) == len(payload) == 50
    assert set(frame["raw_timestamp_ms"]) == {row["time"] for row in payload}
    assert frame["strategy_time_london"].is_monotonic_increasing


def test_offset_is_applied_after_london_conversion_without_mutating_raw_time():
    payload = [
        {
            "product": "COX26-Z26-F27",
            "time": 1788349800000,
            "open": 0.44,
            "high": 0.44,
            "low": 0.44,
            "close": 0.44,
            "volume": 7,
        }
    ]
    corrected = prepare_ohlc_data(payload, timestamp_offset_minutes=65).iloc[0]
    uncorrected = prepare_ohlc_data(payload, timestamp_offset_minutes=0).iloc[0]

    assert corrected["raw_timestamp_ms"] == 1788349800000
    assert corrected["raw_time_london"] == uncorrected["raw_time_london"]
    assert (
        uncorrected["strategy_time_london"] - corrected["strategy_time_london"]
        == pd.to_timedelta(65, unit="min")
    )


def test_missing_bar_is_reported_but_not_created():
    payload = [
        {"product": "X", "time": 0, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"product": "X", "time": 600_000, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 1},
    ]
    frame = prepare_ohlc_data(payload, timestamp_offset_minutes=0)
    gaps = summarize_gaps(frame, "5m")

    assert len(frame) == 2
    assert gaps.gap_count == 1
    assert gaps.missing_intervals == 1


def test_common_qh_intervals_are_understood():
    assert interval_to_timedelta("1M") == pd.to_timedelta(1, unit="min")
    assert interval_to_timedelta("15M") == pd.to_timedelta(15, unit="min")
    assert interval_to_timedelta("1H") == pd.to_timedelta(1, unit="h")
    assert interval_to_timedelta("4H") == pd.to_timedelta(4, unit="h")
    assert interval_to_timedelta("1D") == pd.to_timedelta(1, unit="D")


def test_gap_summary_works_for_non_5m_intervals():
    payload = [
        {"product": "X", "time": 0, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"product": "X", "time": 3_600_000, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 1},
    ]
    frame = prepare_ohlc_data(payload, timestamp_offset_minutes=0)
    gaps = summarize_gaps(frame, "15M")
    assert gaps.gap_count == 1
    assert gaps.missing_intervals == 3


def test_resample_ohlc_data_aggregates_observed_bars_without_filling_empty_buckets():
    from data_utils import resample_ohlc_data

    payload = [
        {"product": "TEST", "time": 1786006800000, "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 2},
        {"product": "TEST", "time": 1786007100000, "open": 10.5, "high": 12.0, "low": 10.0, "close": 11.5, "volume": 3},
        {"product": "TEST", "time": 1786007400000, "open": 11.5, "high": 13.0, "low": 11.0, "close": 12.0, "volume": 4},
        # Deliberately skip the next 15-minute bucket entirely.
        {"product": "TEST", "time": 1786008600000, "open": 20.0, "high": 21.0, "low": 19.0, "close": 20.5, "volume": 5},
    ]
    frame = prepare_ohlc_data(payload, timestamp_offset_minutes=0)
    out = resample_ohlc_data(frame, "15M")

    assert len(out) == 2
    first = out.iloc[0]
    assert first["open"] == 10.0
    assert first["high"] == 13.0
    assert first["low"] == 9.0
    assert first["close"] == 12.0
    assert first["volume"] == 9
    assert first["source_bar_count"] == 3
