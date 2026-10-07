import pandas as pd
import pytest

from strategy_engine import (
    StrategyConfig,
    TrancheSpec,
    calculate_rolling_zscore,
    run_zscore_backtest,
)


def _frame(closes):
    times = pd.date_range("2026-01-01", periods=len(closes), freq="5min", tz="Europe/London")
    return pd.DataFrame({"strategy_time_london": times, "close": closes})


def test_zscore_uses_previous_completed_candles_only():
    data = _frame([10.0, 11.0, 12.0, 20.0])
    result = calculate_rolling_zscore(data, lookback=3)
    # Benchmark at final bar is [10, 11, 12], not a window contaminated by 20.
    assert result.loc[3, "z_mean"] == pytest.approx(11.0)
    assert result.loc[3, "z_std"] == pytest.approx((2.0 / 3.0) ** 0.5)
    assert result.loc[3, "zscore"] > 10.0


def test_short_tranche_enters_and_exits_on_directional_crossings():
    # First 3 points establish the lookback. Then z moves above 1 and later below 0.5.
    data = _frame([0.0, 1.0, 2.0, 1.5, 3.0, 1.5])
    config = StrategyConfig(
        lookback=3,
        tranches=(TrancheSpec(entry_z=1.0, exit_z=0.5, qty=1),),
        tick_size=1.0,
        allow_long=False,
        allow_short=True,
    )
    result = run_zscore_backtest(data, config)
    events = result["events"]
    assert "SELL" in events["action"].tolist()
    assert "BUY" in events["action"].tolist()
    assert len(result["trades"]) == 1


def test_multiple_levels_can_be_crossed_on_one_bar():
    data = _frame([0.0, 1.0, 2.0, 1.5, 10.0])
    config = StrategyConfig(
        lookback=3,
        tranches=(
            TrancheSpec(1.0, 0.5, 1),
            TrancheSpec(2.0, 1.5, 1),
            TrancheSpec(2.5, 2.0, 2),
        ),
        tick_size=1.0,
        allow_long=False,
        allow_short=True,
    )
    result = run_zscore_backtest(data, config)
    active = result["active"]
    assert int(active["qty"].sum()) == 4
    assert set(active["tranche"].tolist()) == {1, 2, 3}


def test_long_side_is_symmetric():
    data = _frame([2.0, 1.0, 0.0, 0.5, -10.0])
    config = StrategyConfig(
        lookback=3,
        tranches=(TrancheSpec(1.0, 0.5, 1),),
        tick_size=1.0,
        allow_long=True,
        allow_short=False,
    )
    result = run_zscore_backtest(data, config)
    assert result["active"].iloc[0]["side"] == "LONG"


def test_short_stop_loss_uses_bar_high_and_records_reason():
    closes = [0.0, 1.0, 2.0, 1.5, 3.0, 2.8, 1.5]
    data = _frame(closes)
    data["open"] = closes
    data["high"] = closes
    data["low"] = closes
    # Short enters at 3.0 on bar 4. A 2-tick stop with tick_size 0.1 is 3.2.
    # The next candle trades to 3.25 even though it closes at 2.8.
    data.loc[5, "high"] = 3.25

    config = StrategyConfig(
        lookback=3,
        tranches=(TrancheSpec(1.0, 0.5, 1, stop_loss_ticks=2.0),),
        tick_size=0.1,
        allow_long=False,
        allow_short=True,
    )
    result = run_zscore_backtest(data, config)
    trade = result["trades"].iloc[0]

    assert trade["exit_reason"] == "STOP LOSS"
    assert trade["stop_price"] == pytest.approx(3.2)
    assert trade["exit_price"] == pytest.approx(3.2)
    assert trade["gross_ticks"] == pytest.approx(-2.0)
    assert result["summary"]["stopouts"] == 1


def test_long_stop_loss_uses_bar_low():
    closes = [2.0, 1.0, 0.0, 0.5, -1.5, -1.3, 0.5]
    data = _frame(closes)
    data["open"] = closes
    data["high"] = closes
    data["low"] = closes
    # Long enters at -1.5. A 2-tick stop with tick_size 0.1 is -1.7.
    data.loc[5, "low"] = -1.75

    config = StrategyConfig(
        lookback=3,
        tranches=(TrancheSpec(1.0, 0.5, 1, stop_loss_ticks=2.0),),
        tick_size=0.1,
        allow_long=True,
        allow_short=False,
    )
    result = run_zscore_backtest(data, config)
    trade = result["trades"].iloc[0]

    assert trade["side"] == "LONG"
    assert trade["exit_reason"] == "STOP LOSS"
    assert trade["stop_price"] == pytest.approx(-1.7)
    assert trade["gross_ticks"] == pytest.approx(-2.0)


def test_gap_through_short_stop_fills_at_worse_open():
    closes = [0.0, 1.0, 2.0, 1.5, 3.0, 3.5]
    data = _frame(closes)
    data["open"] = closes
    data["high"] = closes
    data["low"] = closes
    # Stop is 3.2, but the following bar opens at 3.4 and trades higher.
    data.loc[5, "open"] = 3.4
    data.loc[5, "high"] = 3.6
    data.loc[5, "low"] = 3.3

    config = StrategyConfig(
        lookback=3,
        tranches=(TrancheSpec(1.0, 0.5, 1, stop_loss_ticks=2.0),),
        tick_size=0.1,
        allow_long=False,
        allow_short=True,
    )
    result = run_zscore_backtest(data, config)
    trade = result["trades"].iloc[0]

    assert trade["exit_reason"] == "STOP LOSS"
    assert trade["stop_price"] == pytest.approx(3.2)
    assert trade["exit_price"] == pytest.approx(3.4)
    assert trade["gross_ticks"] == pytest.approx(-4.0)


def test_stop_loss_zero_or_negative_is_rejected_when_enabled():
    with pytest.raises(ValueError):
        TrancheSpec(1.0, 0.5, 1, stop_loss_ticks=0.0)


def test_each_tranche_can_have_an_independent_stop_distance():
    closes = [0.0, 1.0, 2.0, 1.5, 10.0, 9.0]
    data = _frame(closes)
    data["open"] = closes
    data["high"] = closes
    data["low"] = closes
    data.loc[5, "high"] = 10.3

    config = StrategyConfig(
        lookback=3,
        tranches=(
            TrancheSpec(1.0, 0.1, 1, stop_loss_ticks=2.0),
            TrancheSpec(2.0, 0.2, 1, stop_loss_ticks=4.0),
            TrancheSpec(2.5, 0.3, 2, stop_loss_ticks=6.0),
        ),
        tick_size=0.1,
        allow_long=False,
        allow_short=True,
    )
    result = run_zscore_backtest(data, config)

    assert result["summary"]["stopouts"] == 1
    assert result["trades"].iloc[0]["tranche"] == 1
    assert set(result["active"]["tranche"].tolist()) == {2, 3}
    stop_prices = dict(zip(result["active"]["tranche"], result["active"]["stop_price"]))
    assert stop_prices[2] == pytest.approx(10.4)
    assert stop_prices[3] == pytest.approx(10.6)


def test_next_thresholds_can_convert_z_to_price():
    from strategy_engine import next_thresholds

    config = StrategyConfig(
        lookback=3,
        tranches=(TrancheSpec(1.5, 1.0, 1),),
        allow_long=True,
        allow_short=True,
    )
    rows = next_thresholds(config, pd.DataFrame(), mean=0.50, std=0.10)
    short = next(row for row in rows if row["side"] == "SHORT")
    long = next(row for row in rows if row["side"] == "LONG")
    assert short["trigger_price"] == pytest.approx(0.65)
    assert long["trigger_price"] == pytest.approx(0.35)


def test_live_threshold_status_keeps_action_visible_after_crossing():
    from strategy_engine import live_threshold_status

    config = StrategyConfig(
        lookback=3,
        tranches=(TrancheSpec(1.5, 1.0, 1),),
        allow_long=False,
        allow_short=True,
    )
    rows = live_threshold_status(
        config,
        pd.DataFrame(),
        current_z=1.8,
        last_completed_z=1.2,
        mean=0.50,
        std=0.10,
    )
    row = rows[0]
    assert row["action"] == "SELL ENTRY"
    assert row["condition_met"] is True
    assert row["fresh_crossing"] is True
    assert row["trigger_price"] == pytest.approx(0.65)

    # Even after the one-off crossing moment, the action remains visibly actionable
    # as long as the live Z is still beyond the threshold.
    rows = live_threshold_status(
        config,
        pd.DataFrame(),
        current_z=1.7,
        last_completed_z=1.6,
        mean=0.50,
        std=0.10,
    )
    row = rows[0]
    assert row["condition_met"] is True
    assert row["fresh_crossing"] is False


def test_live_threshold_status_uses_active_tranche_for_exit_signal():
    from strategy_engine import live_threshold_status

    config = StrategyConfig(
        lookback=3,
        tranches=(TrancheSpec(1.5, 1.0, 1),),
        allow_long=False,
        allow_short=True,
    )
    active = pd.DataFrame(
        [{"side": "SHORT", "tranche": 1, "stop_price": float("nan")}]
    )
    rows = live_threshold_status(
        config,
        active,
        current_z=0.8,
        last_completed_z=1.2,
        mean=0.50,
        std=0.10,
    )
    row = rows[0]
    assert row["action"] == "BUY EXIT"
    assert row["condition_met"] is True
    assert row["fresh_crossing"] is True
    assert row["trigger_price"] == pytest.approx(0.60)


def test_live_intrabar_tracker_can_enter_and_exit_inside_same_candle():
    from strategy_engine import (
        live_intrabar_active_frame,
        seed_live_intrabar_state,
        step_live_intrabar_state,
    )

    config = StrategyConfig(
        lookback=3,
        tranches=(TrancheSpec(1.5, 1.0, 1),),
        allow_long=False,
        allow_short=True,
    )
    state = seed_live_intrabar_state(config, pd.DataFrame(), previous_z=1.2)

    state, events = step_live_intrabar_state(
        state,
        config,
        current_z=1.7,
        current_price=0.67,
        timestamp=pd.Timestamp("2026-09-07T10:01:00Z"),
    )
    assert [event["action"] for event in events] == ["SELL ENTRY"]
    active = live_intrabar_active_frame(state)
    assert len(active) == 1
    assert active.iloc[0]["side"] == "SHORT"

    state, events = step_live_intrabar_state(
        state,
        config,
        current_z=0.8,
        current_price=0.60,
        timestamp=pd.Timestamp("2026-09-07T10:03:00Z"),
    )
    assert [event["action"] for event in events] == ["BUY EXIT"]
    assert live_intrabar_active_frame(state).empty


def test_live_intrabar_tracker_crosses_multiple_entry_levels():
    from strategy_engine import seed_live_intrabar_state, step_live_intrabar_state

    config = StrategyConfig(
        lookback=3,
        tranches=(
            TrancheSpec(1.5, 1.0, 1),
            TrancheSpec(2.0, 1.5, 1),
            TrancheSpec(2.5, 2.0, 2),
        ),
        allow_long=False,
        allow_short=True,
    )
    state = seed_live_intrabar_state(config, pd.DataFrame(), previous_z=1.2)
    state, events = step_live_intrabar_state(
        state,
        config,
        current_z=2.7,
        current_price=0.80,
        timestamp=pd.Timestamp("2026-09-07T10:01:00Z"),
    )
    assert [event["tranche"] for event in events] == [1, 2, 3]
    assert [event["action"] for event in events] == ["SELL ENTRY", "SELL ENTRY", "SELL ENTRY"]
