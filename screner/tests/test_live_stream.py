from datetime import datetime, timezone

import pandas as pd
import pytest

from live_stream import (
    LiveFeedError,
    append_live_candles_to_history,
    build_live_candles,
    compute_structure_snapshot,
    parse_quoted_structure,
)


def test_parse_three_leg_fly_and_infer_coefficients():
    parsed = parse_quoted_structure("COX26-Z26-F27")
    assert parsed.product == "CO"
    assert parsed.month_codes == ("X26", "Z26", "F27")
    assert parsed.coefficients == (1.0, -2.0, 1.0)


def test_parse_manual_coefficients():
    parsed = parse_quoted_structure("CLX26-Z26", "2,-1")
    assert parsed.coefficients == (2.0, -1.0)


def test_parse_bad_coefficient_count_raises():
    with pytest.raises(LiveFeedError):
        parse_quoted_structure("CLX26-Z26-F27", "1,-1")


def test_structure_snapshot_uses_leg_adjusted_bid_ask():
    quotes = {
        "a": {"AdminPrice": "10", "BidPrice": "9.9", "AskPrice": "10.1"},
        "b": {"AdminPrice": "6", "BidPrice": "5.9", "AskPrice": "6.1"},
        "c": {"AdminPrice": "3", "BidPrice": "2.9", "AskPrice": "3.1"},
    }
    snap = compute_structure_snapshot(quotes, ["a", "b", "c"], [1, -2, 1])
    assert snap is not None
    assert snap["price"] == pytest.approx(1.0)
    assert snap["bid"] == pytest.approx(9.9 - 2 * 6.1 + 2.9)
    assert snap["ask"] == pytest.approx(10.1 - 2 * 5.9 + 3.1)


def test_live_candles_do_not_forward_fill_missing_bucket():
    ticks = pd.DataFrame(
        {
            "received_at_utc": pd.to_datetime(
                ["2026-09-04T10:00:05Z", "2026-09-04T10:00:50Z", "2026-09-04T10:02:10Z"],
                utc=True,
            ),
            "price": [1.0, 1.2, 0.9],
        }
    )
    completed, developing = build_live_candles(
        ticks, "1M", now_utc=datetime(2026, 9, 4, 10, 3, tzinfo=timezone.utc)
    )
    assert developing.empty
    assert list(completed["open"]) == [0.9]
    assert list(completed["close"]) == [0.9]
    assert len(completed) == 1


def test_append_live_only_after_history_end():
    history = pd.DataFrame(
        {
            "product": ["CLX26-Z26"],
            "raw_timestamp_ms": [1],
            "raw_time_utc": pd.to_datetime(["2026-09-04T10:00:00Z"], utc=True),
            "raw_time_london": pd.to_datetime(["2026-09-04T11:00:00+01:00"], utc=True).tz_convert("Europe/London"),
            "strategy_time_london": pd.to_datetime(["2026-09-04T11:00:00+01:00"], utc=True).tz_convert("Europe/London"),
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [1.0],
        }
    )
    live = pd.DataFrame(
        {
            "bucket_utc": pd.to_datetime(["2026-09-04T10:00:00Z", "2026-09-04T10:01:00Z"], utc=True),
            "strategy_time_london": pd.to_datetime(["2026-09-04T11:00:00+01:00", "2026-09-04T11:01:00+01:00"], utc=True).tz_convert("Europe/London"),
            "open": [2.0, 3.0], "high": [2.0, 3.0], "low": [2.0, 3.0], "close": [2.0, 3.0],
            "live_tick_count": [1, 1],
        }
    )
    combined = append_live_candles_to_history(history, live, "CLX26-Z26")
    assert len(combined) == 2
    assert combined.iloc[-1]["close"] == 3.0
