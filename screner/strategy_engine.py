from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TrancheSpec:
    """One independently managed Z-score entry/exit tranche."""

    entry_z: float
    exit_z: float
    qty: int
    stop_loss_ticks: float | None = None

    def __post_init__(self) -> None:
        if self.entry_z <= 0:
            raise ValueError("entry_z must be > 0")
        if self.exit_z < 0:
            raise ValueError("exit_z must be >= 0")
        if self.exit_z >= self.entry_z:
            raise ValueError("exit_z must be below entry_z")
        if self.qty <= 0:
            raise ValueError("qty must be > 0")
        if self.stop_loss_ticks is not None and self.stop_loss_ticks <= 0:
            raise ValueError("stop_loss_ticks must be > 0 when enabled")


@dataclass(frozen=True)
class StrategyConfig:
    lookback: int
    tranches: tuple[TrancheSpec, ...]
    tick_size: float = 0.001
    cost_ticks_per_side: float = 0.0
    allow_long: bool = True
    allow_short: bool = True

    def __post_init__(self) -> None:
        if self.lookback < 2:
            raise ValueError("lookback must be at least 2 candles")
        if not self.tranches:
            raise ValueError("at least one tranche is required")
        if self.tick_size <= 0:
            raise ValueError("tick_size must be > 0")
        if self.cost_ticks_per_side < 0:
            raise ValueError("cost_ticks_per_side cannot be negative")
        entries = [item.entry_z for item in self.tranches]
        if len(set(entries)) != len(entries):
            raise ValueError("entry Z levels must be unique")


@dataclass
class _ActiveTranche:
    side: str
    spec_index: int
    qty: int
    entry_z: float
    exit_z: float
    entry_price: float
    entry_time: pd.Timestamp
    entry_bar: int
    stop_loss_ticks: float | None = None
    stop_price: float | None = None


def _cross_up(previous: float, current: float, level: float) -> bool:
    return previous < level <= current


def _cross_down(previous: float, current: float, level: float) -> bool:
    return previous > level >= current


def _finite_number(value: object, fallback: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return fallback
    return numeric if np.isfinite(numeric) else fallback


def calculate_rolling_zscore(
    frame: pd.DataFrame,
    lookback: int,
    price_column: str = "close",
) -> pd.DataFrame:
    """
    Calculate a non-self-contaminating rolling Z-score.

    The benchmark mean/std at bar t uses only the previous `lookback` completed
    observations. The current bar's price is then standardized against that frozen
    benchmark. Missing market intervals are never created or forward-filled.
    """
    if lookback < 2:
        raise ValueError("lookback must be at least 2 candles")
    if price_column not in frame.columns:
        raise ValueError(f"Missing price column: {price_column}")

    result = frame.copy()
    price = pd.to_numeric(result[price_column], errors="coerce")
    history = price.shift(1)
    mean = history.rolling(lookback, min_periods=lookback).mean()
    std = history.rolling(lookback, min_periods=lookback).std(ddof=0)
    std = std.mask(std <= 0)

    result["z_mean"] = mean
    result["z_std"] = std
    result["zscore"] = (price - mean) / std
    return result


def _stop_fill_price(
    active: _ActiveTranche,
    row: pd.Series,
    close_price: float,
) -> float | None:
    """Return a simulated stop fill price if the completed bar touched the stop.

    Stops are checked only on bars after the entry bar. High/low determine whether
    the stop was touched. If the next bar opens through the stop, the open is used
    as a conservative gap fill; otherwise the configured stop price is used.
    """
    if active.stop_price is None:
        return None

    open_price = _finite_number(row.get("open"), close_price)
    high_price = _finite_number(row.get("high"), close_price)
    low_price = _finite_number(row.get("low"), close_price)

    if active.side == "SHORT" and high_price >= active.stop_price:
        return max(active.stop_price, open_price)
    if active.side == "LONG" and low_price <= active.stop_price:
        return min(active.stop_price, open_price)
    return None


def _close_trade(
    active: _ActiveTranche,
    exit_price: float,
    exit_z_value: float | None,
    exit_time: pd.Timestamp,
    exit_bar: int,
    config: StrategyConfig,
    *,
    exit_reason: str,
) -> dict[str, object]:
    direction = 1.0 if active.side == "LONG" else -1.0
    gross_price_pnl = direction * (exit_price - active.entry_price) * active.qty
    gross_ticks = gross_price_pnl / config.tick_size
    costs = 2.0 * config.cost_ticks_per_side * active.qty
    net_ticks = gross_ticks - costs
    return {
        "side": active.side,
        "tranche": active.spec_index + 1,
        "qty": active.qty,
        "entry_time": active.entry_time,
        "exit_time": exit_time,
        "entry_price": active.entry_price,
        "exit_price": exit_price,
        "entry_z": active.entry_z if active.side == "SHORT" else -active.entry_z,
        "exit_trigger_z": active.exit_z if active.side == "SHORT" else -active.exit_z,
        "exit_z": exit_z_value if exit_z_value is not None else np.nan,
        "stop_loss_ticks": active.stop_loss_ticks,
        "stop_price": active.stop_price,
        "exit_reason": exit_reason,
        "holding_bars": exit_bar - active.entry_bar,
        "gross_ticks": gross_ticks,
        "cost_ticks": costs,
        "net_ticks": net_ticks,
        "result": "WIN" if net_ticks > 0 else ("LOSS" if net_ticks < 0 else "FLAT"),
    }


def _new_active_tranche(
    *,
    side: str,
    spec_index: int,
    spec: TrancheSpec,
    price: float,
    timestamp: pd.Timestamp,
    bar_index: int,
    tick_size: float,
) -> _ActiveTranche:
    if spec.stop_loss_ticks is None:
        stop_price = None
    elif side == "SHORT":
        stop_price = price + spec.stop_loss_ticks * tick_size
    else:
        stop_price = price - spec.stop_loss_ticks * tick_size

    return _ActiveTranche(
        side=side,
        spec_index=spec_index,
        qty=spec.qty,
        entry_z=spec.entry_z,
        exit_z=spec.exit_z,
        entry_price=price,
        entry_time=timestamp,
        entry_bar=bar_index,
        stop_loss_ticks=spec.stop_loss_ticks,
        stop_price=stop_price,
    )


def run_zscore_backtest(
    frame: pd.DataFrame,
    config: StrategyConfig,
    *,
    time_column: str = "strategy_time_london",
    price_column: str = "close",
) -> dict[str, object]:
    """Run the tranche state machine on completed bars.

    Existing stop losses are checked first using completed-bar OHLC. Z-score exits
    are then evaluated, followed by new entries. This stop-first ordering is a
    conservative assumption when an OHLC candle does not reveal intrabar sequence.
    """
    if time_column not in frame.columns:
        raise ValueError(f"Missing time column: {time_column}")

    data = calculate_rolling_zscore(frame, config.lookback, price_column)
    data = data.reset_index(drop=True)

    active: dict[tuple[str, int], _ActiveTranche] = {}
    trades: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    states: list[dict[str, object]] = []
    realized_net_ticks = 0.0

    previous_z: float | None = None
    sorted_specs = sorted(enumerate(config.tranches), key=lambda pair: pair[1].entry_z)

    for bar_index, row in data.iterrows():
        z_raw = row["zscore"]
        price = float(row[price_column])
        timestamp = row[time_column]
        z = float(z_raw) if pd.notna(z_raw) and np.isfinite(z_raw) else None
        stopped_this_bar: set[tuple[str, int]] = set()

        # 1) Price/tick stop-losses. A stop is not checked on its own entry bar,
        # because the research build assumes the entry fill occurs at bar close.
        for side, spec_index in list(active.keys()):
            position = active[(side, spec_index)]
            if bar_index <= position.entry_bar:
                continue
            stop_fill = _stop_fill_price(position, row, price)
            if stop_fill is None:
                continue

            trade = _close_trade(
                position,
                stop_fill,
                z,
                timestamp,
                bar_index,
                config,
                exit_reason="STOP LOSS",
            )
            realized_net_ticks += float(trade["net_ticks"])
            trades.append(trade)
            events.append(
                {
                    "time": timestamp,
                    "zscore": z,
                    "price": stop_fill,
                    "action": "BUY" if side == "SHORT" else "SELL",
                    "reason": f"Stop loss {side.lower()} tranche {spec_index + 1}",
                    "qty": position.qty,
                    "tranche": spec_index + 1,
                    "exit_reason": "STOP LOSS",
                }
            )
            stopped_this_bar.add((side, spec_index))
            del active[(side, spec_index)]

        if z is not None and previous_z is not None:
            # 2) Z-score exits for positions that survived the stop check.
            for side, spec_index in list(active.keys()):
                position = active[(side, spec_index)]
                if side == "SHORT":
                    exit_level = position.exit_z
                    should_exit = _cross_down(previous_z, z, exit_level)
                else:
                    exit_level = -position.exit_z
                    should_exit = _cross_up(previous_z, z, exit_level)

                if should_exit:
                    trade = _close_trade(
                        position,
                        price,
                        z,
                        timestamp,
                        bar_index,
                        config,
                        exit_reason="Z EXIT",
                    )
                    realized_net_ticks += float(trade["net_ticks"])
                    trades.append(trade)
                    events.append(
                        {
                            "time": timestamp,
                            "zscore": z,
                            "price": price,
                            "action": "BUY" if side == "SHORT" else "SELL",
                            "reason": f"Exit {side.lower()} tranche {spec_index + 1}",
                            "qty": position.qty,
                            "tranche": spec_index + 1,
                            "exit_reason": "Z EXIT",
                        }
                    )
                    del active[(side, spec_index)]

            # 3) New entries. Each tranche can be active at most once per side.
            # A tranche stopped on this bar cannot immediately re-enter on the same
            # completed candle; it must see a fresh threshold crossing later.
            for spec_index, spec in sorted_specs:
                if config.allow_short and ("SHORT", spec_index) not in active:
                    if ("SHORT", spec_index) not in stopped_this_bar and _cross_up(
                        previous_z, z, spec.entry_z
                    ):
                        active[("SHORT", spec_index)] = _new_active_tranche(
                            side="SHORT",
                            spec_index=spec_index,
                            spec=spec,
                            price=price,
                            timestamp=timestamp,
                            bar_index=bar_index,
                            tick_size=config.tick_size,
                        )
                        events.append(
                            {
                                "time": timestamp,
                                "zscore": z,
                                "price": price,
                                "action": "SELL",
                                "reason": f"Enter short tranche {spec_index + 1}",
                                "qty": spec.qty,
                                "tranche": spec_index + 1,
                                "exit_reason": "",
                            }
                        )

                if config.allow_long and ("LONG", spec_index) not in active:
                    long_level = -spec.entry_z
                    if ("LONG", spec_index) not in stopped_this_bar and _cross_down(
                        previous_z, z, long_level
                    ):
                        active[("LONG", spec_index)] = _new_active_tranche(
                            side="LONG",
                            spec_index=spec_index,
                            spec=spec,
                            price=price,
                            timestamp=timestamp,
                            bar_index=bar_index,
                            tick_size=config.tick_size,
                        )
                        events.append(
                            {
                                "time": timestamp,
                                "zscore": z,
                                "price": price,
                                "action": "BUY",
                                "reason": f"Enter long tranche {spec_index + 1}",
                                "qty": spec.qty,
                                "tranche": spec_index + 1,
                                "exit_reason": "",
                            }
                        )

        long_qty = sum(pos.qty for pos in active.values() if pos.side == "LONG")
        short_qty = sum(pos.qty for pos in active.values() if pos.side == "SHORT")
        signed_position = long_qty - short_qty

        unrealized_gross_ticks = 0.0
        open_entry_cost_ticks = 0.0
        for pos in active.values():
            direction = 1.0 if pos.side == "LONG" else -1.0
            unrealized_gross_ticks += (
                direction * (price - pos.entry_price) * pos.qty / config.tick_size
            )
            open_entry_cost_ticks += config.cost_ticks_per_side * pos.qty

        equity_ticks = realized_net_ticks + unrealized_gross_ticks - open_entry_cost_ticks
        states.append(
            {
                "time": timestamp,
                "price": price,
                "zscore": z,
                "position": signed_position,
                "long_qty": long_qty,
                "short_qty": short_qty,
                "realized_net_ticks": realized_net_ticks,
                "unrealized_gross_ticks": unrealized_gross_ticks,
                "equity_ticks": equity_ticks,
            }
        )

        if z is not None:
            previous_z = z

    trade_frame = pd.DataFrame(trades)
    event_frame = pd.DataFrame(events)
    state_frame = pd.DataFrame(states)

    if not state_frame.empty:
        running_peak = state_frame["equity_ticks"].cummax()
        drawdown = state_frame["equity_ticks"] - running_peak
        max_drawdown = abs(float(drawdown.min()))
    else:
        max_drawdown = 0.0

    if trade_frame.empty:
        wins = losses = 0
        win_rate = 0.0
        avg_win = avg_loss = 0.0
        stopouts = z_exits = 0
        stopout_net_ticks = z_exit_net_ticks = 0.0
        total_cost_ticks = 0.0
    else:
        wins = int((trade_frame["net_ticks"] > 0).sum())
        losses = int((trade_frame["net_ticks"] < 0).sum())
        completed = len(trade_frame)
        win_rate = 100.0 * wins / completed if completed else 0.0
        avg_win = (
            float(trade_frame.loc[trade_frame["net_ticks"] > 0, "net_ticks"].mean())
            if wins
            else 0.0
        )
        avg_loss = (
            float(trade_frame.loc[trade_frame["net_ticks"] < 0, "net_ticks"].mean())
            if losses
            else 0.0
        )
        stop_mask = trade_frame["exit_reason"] == "STOP LOSS"
        z_mask = trade_frame["exit_reason"] == "Z EXIT"
        stopouts = int(stop_mask.sum())
        z_exits = int(z_mask.sum())
        stopout_net_ticks = float(trade_frame.loc[stop_mask, "net_ticks"].sum())
        z_exit_net_ticks = float(trade_frame.loc[z_mask, "net_ticks"].sum())
        total_cost_ticks = float(trade_frame["cost_ticks"].sum())

    active_rows: list[dict[str, object]] = []
    latest_price = float(data[price_column].iloc[-1]) if not data.empty else np.nan
    latest_z = data["zscore"].iloc[-1] if not data.empty else np.nan
    for pos in sorted(active.values(), key=lambda item: (item.side, item.spec_index)):
        direction = 1.0 if pos.side == "LONG" else -1.0
        unrealized_ticks = direction * (latest_price - pos.entry_price) * pos.qty / config.tick_size
        active_rows.append(
            {
                "side": pos.side,
                "tranche": pos.spec_index + 1,
                "qty": pos.qty,
                "entry_time": pos.entry_time,
                "entry_price": pos.entry_price,
                "entry_z": pos.entry_z if pos.side == "SHORT" else -pos.entry_z,
                "exit_trigger_z": pos.exit_z if pos.side == "SHORT" else -pos.exit_z,
                "stop_loss_ticks": pos.stop_loss_ticks,
                "stop_price": pos.stop_price,
                "latest_z": float(latest_z) if pd.notna(latest_z) else np.nan,
                "unrealized_ticks": unrealized_ticks,
            }
        )
    active_frame = pd.DataFrame(active_rows)

    summary = {
        "completed_trades": int(len(trade_frame)),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
        "net_ticks": float(trade_frame["net_ticks"].sum()) if not trade_frame.empty else 0.0,
        "gross_ticks": float(trade_frame["gross_ticks"].sum()) if not trade_frame.empty else 0.0,
        "total_cost_ticks": total_cost_ticks,
        "avg_win_ticks": avg_win,
        "avg_loss_ticks": avg_loss,
        "max_drawdown_ticks": max_drawdown,
        "max_abs_position": int(state_frame["position"].abs().max()) if not state_frame.empty else 0,
        "open_tranches": int(len(active_frame)),
        "open_unrealized_ticks": float(active_frame["unrealized_ticks"].sum()) if not active_frame.empty else 0.0,
        "latest_z": float(latest_z) if pd.notna(latest_z) else np.nan,
        "stopouts": stopouts,
        "stopout_net_ticks": stopout_net_ticks,
        "z_exits": z_exits,
        "z_exit_net_ticks": z_exit_net_ticks,
    }

    return {
        "data": data,
        "trades": trade_frame,
        "events": event_frame,
        "states": state_frame,
        "active": active_frame,
        "summary": summary,
    }


def _trigger_price_from_z(mean: float | None, std: float | None, z_level: float) -> float:
    """Convert a Z threshold back to a structure-price threshold when possible."""
    try:
        mean_value = float(mean) if mean is not None else np.nan
        std_value = float(std) if std is not None else np.nan
    except (TypeError, ValueError):
        return np.nan
    if not np.isfinite(mean_value) or not np.isfinite(std_value) or std_value <= 0:
        return np.nan
    return float(mean_value + float(z_level) * std_value)


def next_thresholds(
    config: StrategyConfig,
    active: pd.DataFrame,
    *,
    mean: float | None = None,
    std: float | None = None,
) -> list[dict[str, object]]:
    """Describe currently relevant add, Z-exit, and stop thresholds.

    If the current frozen benchmark mean/std are supplied, Z thresholds are also
    converted to their equivalent structure prices. This makes the live panel
    actionable without changing the underlying Z logic.
    """
    active_lookup: dict[tuple[str, int], object] = {}
    if not active.empty:
        for row in active.itertuples(index=False):
            active_lookup[(str(row.side), int(row.tranche) - 1)] = row

    rows: list[dict[str, object]] = []
    for index, spec in sorted(enumerate(config.tranches), key=lambda pair: pair[1].entry_z):
        if config.allow_short:
            key = ("SHORT", index)
            if key in active_lookup:
                active_row = active_lookup[key]
                z_level = spec.exit_z
                rows.append(
                    {
                        "side": "SHORT",
                        "tranche": index + 1,
                        "action": "BUY Z EXIT",
                        "trigger_z": z_level,
                        "trigger_price": _trigger_price_from_z(mean, std, z_level),
                        "qty": spec.qty,
                    }
                )
                if spec.stop_loss_ticks is not None:
                    rows.append(
                        {
                            "side": "SHORT",
                            "tranche": index + 1,
                            "action": "BUY STOP",
                            "trigger_z": np.nan,
                            "trigger_price": float(active_row.stop_price),
                            "qty": spec.qty,
                        }
                    )
            else:
                z_level = spec.entry_z
                rows.append(
                    {
                        "side": "SHORT",
                        "tranche": index + 1,
                        "action": "SELL ADD",
                        "trigger_z": z_level,
                        "trigger_price": _trigger_price_from_z(mean, std, z_level),
                        "qty": spec.qty,
                    }
                )
        if config.allow_long:
            key = ("LONG", index)
            if key in active_lookup:
                active_row = active_lookup[key]
                z_level = -spec.exit_z
                rows.append(
                    {
                        "side": "LONG",
                        "tranche": index + 1,
                        "action": "SELL Z EXIT",
                        "trigger_z": z_level,
                        "trigger_price": _trigger_price_from_z(mean, std, z_level),
                        "qty": spec.qty,
                    }
                )
                if spec.stop_loss_ticks is not None:
                    rows.append(
                        {
                            "side": "LONG",
                            "tranche": index + 1,
                            "action": "SELL STOP",
                            "trigger_z": np.nan,
                            "trigger_price": float(active_row.stop_price),
                            "qty": spec.qty,
                        }
                    )
            else:
                z_level = -spec.entry_z
                rows.append(
                    {
                        "side": "LONG",
                        "tranche": index + 1,
                        "action": "BUY ADD",
                        "trigger_z": z_level,
                        "trigger_price": _trigger_price_from_z(mean, std, z_level),
                        "qty": spec.qty,
                    }
                )
    return rows


def live_threshold_status(
    config: StrategyConfig,
    active: pd.DataFrame,
    *,
    current_z: float,
    last_completed_z: float | None = None,
    mean: float | None = None,
    std: float | None = None,
) -> list[dict[str, object]]:
    """Return continuous intrabar entry/exit status for the current developing candle.

    The completed-candle model remains the source of the active tranche state. The
    current developing-candle Z is then checked against each relevant threshold.
    `condition_met` stays true while an action is actionable, so the UI does not
    hide the BUY/SELL instruction merely because a one-off crossing event was missed.
    `fresh_crossing` compares the last completed selected-timeframe Z with the live Z
    and therefore has the same time basis as the selected strategy candle.
    """
    try:
        z_now = float(current_z)
    except (TypeError, ValueError):
        return []
    if not np.isfinite(z_now):
        return []

    try:
        z_prev = float(last_completed_z) if last_completed_z is not None else np.nan
    except (TypeError, ValueError):
        z_prev = np.nan

    active_lookup: set[tuple[str, int]] = set()
    if active is not None and not active.empty:
        for row in active.itertuples(index=False):
            active_lookup.add((str(row.side), int(row.tranche) - 1))

    rows: list[dict[str, object]] = []
    for index, spec in sorted(enumerate(config.tranches), key=lambda pair: pair[1].entry_z):
        if config.allow_short:
            key = ("SHORT", index)
            if key in active_lookup:
                trigger_z = float(spec.exit_z)
                met = z_now <= trigger_z
                fresh = bool(np.isfinite(z_prev) and z_prev > trigger_z >= z_now)
                action = "BUY EXIT"
                state = "SHORT ACTIVE"
                distance = max(0.0, z_now - trigger_z)
            else:
                trigger_z = float(spec.entry_z)
                met = z_now >= trigger_z
                fresh = bool(np.isfinite(z_prev) and z_prev < trigger_z <= z_now)
                action = "SELL ENTRY"
                state = "SHORT FLAT"
                distance = max(0.0, trigger_z - z_now)
            rows.append(
                {
                    "side": "SHORT",
                    "tranche": index + 1,
                    "state": state,
                    "action": action,
                    "condition_met": bool(met),
                    "fresh_crossing": fresh,
                    "current_z": z_now,
                    "trigger_z": trigger_z,
                    "trigger_price": _trigger_price_from_z(mean, std, trigger_z),
                    "distance_z": float(distance),
                    "qty": spec.qty,
                }
            )

        if config.allow_long:
            key = ("LONG", index)
            if key in active_lookup:
                trigger_z = float(-spec.exit_z)
                met = z_now >= trigger_z
                fresh = bool(np.isfinite(z_prev) and z_prev < trigger_z <= z_now)
                action = "SELL EXIT"
                state = "LONG ACTIVE"
                distance = max(0.0, trigger_z - z_now)
            else:
                trigger_z = float(-spec.entry_z)
                met = z_now <= trigger_z
                fresh = bool(np.isfinite(z_prev) and z_prev > trigger_z >= z_now)
                action = "BUY ENTRY"
                state = "LONG FLAT"
                distance = max(0.0, z_now - trigger_z)
            rows.append(
                {
                    "side": "LONG",
                    "tranche": index + 1,
                    "state": state,
                    "action": action,
                    "condition_met": bool(met),
                    "fresh_crossing": fresh,
                    "current_z": z_now,
                    "trigger_z": trigger_z,
                    "trigger_price": _trigger_price_from_z(mean, std, trigger_z),
                    "distance_z": float(distance),
                    "qty": spec.qty,
                }
            )
    return rows


def seed_live_intrabar_state(
    config: StrategyConfig,
    active: pd.DataFrame,
    *,
    previous_z: float | None,
) -> dict[str, object]:
    """Seed the live intrabar signal state from the last completed-candle model state."""
    positions: dict[str, dict[str, object]] = {}
    if active is not None and not active.empty:
        for row in active.itertuples(index=False):
            side = str(row.side)
            spec_index = int(row.tranche) - 1
            key = f"{side}:{spec_index}"
            positions[key] = {
                "side": side,
                "spec_index": spec_index,
                "tranche": spec_index + 1,
                "qty": int(row.qty),
                "entry_price": float(row.entry_price),
                "entry_z": float(row.entry_z),
                "stop_price": None
                if pd.isna(row.stop_price)
                else float(row.stop_price),
                "source": "completed model",
            }
    try:
        z_prev = float(previous_z) if previous_z is not None else np.nan
    except (TypeError, ValueError):
        z_prev = np.nan
    return {
        "previous_z": z_prev,
        "active": positions,
        "events": [],
    }


def live_intrabar_active_frame(state: dict[str, object]) -> pd.DataFrame:
    active = state.get("active") if isinstance(state, dict) else None
    if not isinstance(active, dict) or not active:
        return pd.DataFrame()
    rows = list(active.values())
    return pd.DataFrame(rows).sort_values(["side", "tranche"]).reset_index(drop=True)


def step_live_intrabar_state(
    state: dict[str, object],
    config: StrategyConfig,
    *,
    current_z: float,
    current_price: float,
    timestamp: object,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Advance the live Z crossing state using every displayed live-Z update.

    This is intentionally separate from the completed-candle backtest. It lets the
    live monitor produce entry *and subsequent exit* signals inside the same selected
    candle. No exchange orders are sent.
    """
    try:
        z_now = float(current_z)
        price = float(current_price)
    except (TypeError, ValueError):
        return state, []
    if not np.isfinite(z_now) or not np.isfinite(price):
        return state, []

    try:
        z_prev = float(state.get("previous_z", np.nan))
    except (TypeError, ValueError):
        z_prev = np.nan
    active = state.setdefault("active", {})
    if not isinstance(active, dict):
        active = {}
        state["active"] = active

    events: list[dict[str, object]] = []
    if np.isfinite(z_prev):
        # 1) Existing live Z exits first.
        for key in list(active.keys()):
            pos = active[key]
            side = str(pos["side"])
            spec_index = int(pos["spec_index"])
            spec = config.tranches[spec_index]
            if side == "SHORT":
                trigger_z = float(spec.exit_z)
                crossed = _cross_down(z_prev, z_now, trigger_z)
                action = "BUY EXIT"
            else:
                trigger_z = float(-spec.exit_z)
                crossed = _cross_up(z_prev, z_now, trigger_z)
                action = "SELL EXIT"
            if crossed:
                events.append(
                    {
                        "time": timestamp,
                        "action": action,
                        "side": side,
                        "tranche": spec_index + 1,
                        "qty": int(pos["qty"]),
                        "price": price,
                        "zscore": z_now,
                        "trigger_z": trigger_z,
                        "reason": "LIVE Z EXIT CROSSING",
                    }
                )
                del active[key]

        # 2) New live entries. Multiple tranche thresholds may be crossed in one update.
        for spec_index, spec in sorted(enumerate(config.tranches), key=lambda pair: pair[1].entry_z):
            short_key = f"SHORT:{spec_index}"
            if config.allow_short and short_key not in active and _cross_up(z_prev, z_now, spec.entry_z):
                stop_price = (
                    None
                    if spec.stop_loss_ticks is None
                    else price + spec.stop_loss_ticks * config.tick_size
                )
                active[short_key] = {
                    "side": "SHORT",
                    "spec_index": spec_index,
                    "tranche": spec_index + 1,
                    "qty": spec.qty,
                    "entry_price": price,
                    "entry_z": z_now,
                    "stop_price": stop_price,
                    "source": "live intrabar",
                }
                events.append(
                    {
                        "time": timestamp,
                        "action": "SELL ENTRY",
                        "side": "SHORT",
                        "tranche": spec_index + 1,
                        "qty": spec.qty,
                        "price": price,
                        "zscore": z_now,
                        "trigger_z": float(spec.entry_z),
                        "reason": "LIVE Z ENTRY CROSSING",
                    }
                )

            long_key = f"LONG:{spec_index}"
            long_level = -spec.entry_z
            if config.allow_long and long_key not in active and _cross_down(z_prev, z_now, long_level):
                stop_price = (
                    None
                    if spec.stop_loss_ticks is None
                    else price - spec.stop_loss_ticks * config.tick_size
                )
                active[long_key] = {
                    "side": "LONG",
                    "spec_index": spec_index,
                    "tranche": spec_index + 1,
                    "qty": spec.qty,
                    "entry_price": price,
                    "entry_z": z_now,
                    "stop_price": stop_price,
                    "source": "live intrabar",
                }
                events.append(
                    {
                        "time": timestamp,
                        "action": "BUY ENTRY",
                        "side": "LONG",
                        "tranche": spec_index + 1,
                        "qty": spec.qty,
                        "price": price,
                        "zscore": z_now,
                        "trigger_z": float(long_level),
                        "reason": "LIVE Z ENTRY CROSSING",
                    }
                )

    state["previous_z"] = z_now
    if events:
        history = state.setdefault("events", [])
        if not isinstance(history, list):
            history = []
            state["events"] = history
        history.extend(events)
        if len(history) > 200:
            del history[:-200]
    return state, events
