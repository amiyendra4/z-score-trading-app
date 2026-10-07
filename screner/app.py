from __future__ import annotations

import truststore

# Use the OS-managed certificate authorities, including corporate roots.
# Bootstrap before HTTP clients import their SSL contexts; verification stays on.
truststore.inject_into_ssl()

import json
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from auth_utils import (
    QH_AUTH_URL,
    TokenInputError,
    delete_saved_token,
    inspect_saved_token,
    save_access_token,
)
from chart_component import render_ohlc_chart
from data_utils import DataValidationError, interval_to_timedelta, prepare_ohlc_data, resample_ohlc_data, summarize_gaps
from qh_client import QHAPIError, QHAuthenticationError, QHClient
from live_stream import (
    LiveAdminPriceHub,
    LiveFeedError,
    append_live_candles_to_history,
    build_live_candles,
    coefficients_text_for_instrument,
    parse_quoted_structure,
    resolve_structure_items,
)
from insight_prices import DEFAULT_INSIGHT_URL, InsightPriceError, fetch_insight, build_insight_snapshot
from insight_panel import render_insight_panel
from chart_zscore import render_chart_zscore
from qh_direct import fetch_direct_structure, split_direct_candles, render_qh_direct_panel

from strategy_engine import (
    StrategyConfig,
    TrancheSpec,
    live_intrabar_active_frame,
    live_threshold_status,
    next_thresholds,
    run_zscore_backtest,
    seed_live_intrabar_state,
    step_live_intrabar_state,
)


APP_DIR = Path(__file__).resolve().parent
SAMPLE_FILE = APP_DIR / "sample_data" / "response_1788346938262.json"
AUTH_FILE = APP_DIR / ".secrets" / "qh_authorization.txt"

# Strategy interval presets. Some QH deployments do not expose every interval
# as a native table (notably 15M). For those cases we fetch a smaller real QH
# interval and aggregate locally; empty buckets remain absent.
INTERVAL_PRESETS = ["1M", "5M", "15M", "30M", "1H", "4H", "1D", "Custom"]
QH_BASE_INTERVALS = {
    "15M": "5M",
    "30M": "5M",
    "4H": "1H",
}

st.set_page_config(
    page_title="Screner",
    page_icon=":material/candlestick_chart:",
    layout="wide",
)


@st.cache_data(ttl="1h", max_entries=10, show_spinner=False)
def load_json_file(path: str) -> object:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _qh_fetch_plan(strategy_interval: str, requested_bars: int) -> tuple[str, int]:
    """Choose the QH source table and enough source bars for the strategy interval."""
    source_interval = QH_BASE_INTERVALS.get(strategy_interval.upper(), strategy_interval.upper())
    target_td = interval_to_timedelta(strategy_interval)
    source_td = interval_to_timedelta(source_interval)
    multiplier = 1
    if target_td is not None and source_td is not None and target_td > source_td:
        multiplier = max(1, int(target_td / source_td))
    # QH control in this app caps requests at 10,000 bars. Add one source bucket
    # so boundary aggregation does not unnecessarily shorten the requested history.
    source_count = min(10_000, max(1, int(requested_bars) * multiplier + multiplier))
    return source_interval, source_count


@st.cache_data(ttl="60s", max_entries=50, show_spinner=False)
def fetch_api_data(
    instrument: str,
    strategy_interval: str,
    count: int,
    refresh_key: int = 0,
) -> tuple[object, dict[str, str | None], bool, str]:
    client = QHClient(auth_file=AUTH_FILE, cache_dir=APP_DIR / ".cache")
    source_interval, source_count = _qh_fetch_plan(strategy_interval, count)
    try:
        result = client.fetch_ohlc(
            instruments=instrument,
            interval=source_interval,
            count=source_count,
            cache_ttl_seconds=0 if refresh_key else 60,
        )
    except QHAPIError as exc:
        # Generic fallback for a deployment where a selected interval is not a native table.
        # 5M is the known working intraday base in the user's QH environment.
        message = str(exc).lower()
        if "invalid table" not in message or source_interval.upper() in {"1M", "5M"}:
            raise
        source_interval = "5M"
        target_td = interval_to_timedelta(strategy_interval)
        source_td = interval_to_timedelta(source_interval)
        multiplier = max(1, int(target_td / source_td)) if target_td and source_td and target_td >= source_td else 1
        source_count = min(10_000, max(1, int(count) * multiplier + multiplier))
        result = client.fetch_ohlc(
            instruments=instrument,
            interval=source_interval,
            count=source_count,
            cache_ttl_seconds=0 if refresh_key else 60,
        )
    return result.payload, result.rate_limit, result.from_cache, source_interval


def render_qh_access_panel() -> bool:
    """Render the no-file-edit QH token workflow and return whether API use can continue."""
    st.subheader("QH access")
    status = inspect_saved_token(AUTH_FILE)

    if not status.exists:
        st.warning("No QH access token saved.")
    elif not status.usable:
        st.error(status.message)
    elif status.expiry_known and (status.seconds_remaining or 0) <= 10 * 60:
        st.warning(status.message)
    else:
        st.success(status.message)

    st.link_button(
        "1. Open QH Microsoft sign-in",
        QH_AUTH_URL,
        icon=":material/login:",
        use_container_width=True,
        help="Sign in with Microsoft. QH will return an access value / auth response.",
    )

    with st.form("qh_access_token_form", clear_on_submit=True):
        pasted = st.text_input(
            "2. Paste QH access value or full auth JSON",
            type="password",
            placeholder="Paste access token / Bearer token / full JSON here",
            help=(
                "You can paste the raw access value, 'Bearer <token>', or the complete JSON "
                "returned by the QH auth page. The app extracts and saves only the access token."
            ),
        )
        save_clicked = st.form_submit_button(
            "Save & use access token",
            icon=":material/save:",
            use_container_width=True,
        )

    if save_clicked:
        try:
            save_access_token(AUTH_FILE, pasted)
        except (OSError, TokenInputError) as exc:
            st.error(str(exc))
        else:
            fetch_api_data.clear()
            st.toast("QH access token saved.", icon=":material/check_circle:")
            st.rerun()

    if status.exists:
        with st.expander("Access token options"):
            if status.preview:
                st.caption(f"Saved token: {status.preview}")
            if st.button(
                "Remove saved token",
                icon=":material/delete:",
                use_container_width=True,
            ):
                delete_saved_token(AUTH_FILE)
                fetch_api_data.clear()
                st.rerun()

    st.caption(
        "When QH expires the token, repeat steps 1–2. You no longer need to find or edit "
        "qh_authorization.txt yourself."
    )
    return inspect_saved_token(AUTH_FILE).usable


def _format_signed(value: float, decimals: int = 2) -> str:
    if not np.isfinite(value):
        return "—"
    return f"{value:+.{decimals}f}"


def get_live_hub() -> LiveAdminPriceHub:
    # Keep one independent market-data connection per Streamlit browser session.
    if "_live_admin_price_hub" not in st.session_state:
        st.session_state._live_admin_price_hub = LiveAdminPriceHub()
    return st.session_state._live_admin_price_hub


@st.cache_data(ttl="1h", max_entries=50, show_spinner=False)
def resolve_live_data(instrument: str, coefficients: str):
    return resolve_structure_items(instrument, coefficients)


def _live_zscore(history: pd.DataFrame, lookback: int, live_price: float) -> tuple[float, float, float]:
    closes = pd.to_numeric(history["close"], errors="coerce").dropna()
    if len(closes) < lookback:
        return float("nan"), float("nan"), float("nan")
    benchmark = closes.iloc[-lookback:]
    mean = float(benchmark.mean())
    std = float(benchmark.std(ddof=0))
    if not np.isfinite(std) or std <= 0:
        return mean, std, float("nan")
    return mean, std, float((live_price - mean) / std)


def _format_age(timestamp) -> str:
    if timestamp is None:
        return "—"
    now = pd.Timestamp.now(tz="UTC")
    value = pd.Timestamp(timestamp)
    if value.tzinfo is None:
        value = value.tz_localize("UTC")
    else:
        value = value.tz_convert("UTC")
    age = max(0.0, (now - value).total_seconds())
    return f"{age:.1f}s ago" if age < 60 else f"{age / 60:.1f}m ago"


def _format_countdown(seconds: float) -> str:
    seconds = max(0, int(round(float(seconds))))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _developing_candle_clock(developing: pd.DataFrame, interval_api: str) -> dict[str, object] | None:
    if developing is None or developing.empty:
        return None
    duration = interval_to_timedelta(interval_api)
    if duration is None or duration <= pd.Timedelta(0):
        return None
    row = developing.iloc[-1]
    start_utc = pd.Timestamp(row["bucket_utc"])
    if start_utc.tzinfo is None:
        start_utc = start_utc.tz_localize("UTC")
    else:
        start_utc = start_utc.tz_convert("UTC")
    end_utc = start_utc + duration
    now_utc = pd.Timestamp.now(tz="UTC")
    seconds_left = max(0.0, (end_utc - now_utc).total_seconds())
    return {
        "start_utc": start_utc,
        "end_utc": end_utc,
        "start_london": start_utc.tz_convert("Europe/London"),
        "end_london": end_utc.tz_convert("Europe/London"),
        "seconds_left": seconds_left,
    }


def _build_strategy_config() -> StrategyConfig:
    st.subheader("Strategy parameters")
    st.caption(
        "Z is calculated from the current close versus the previous X completed candles. "
        "The current candle does not enter its own mean/std benchmark."
    )

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        lookback = int(st.session_state.get("z_lookback", 40))
        st.metric("Z lookback (candles)", lookback)
        st.caption("Set the candle count above the Price / OHLC chart.")
    with c2:
        tick_size = st.number_input(
            "Tick size",
            min_value=0.000001,
            value=0.001,
            step=0.001,
            format="%.6f",
            key="z_tick_size",
            help="Used only to convert price P&L into ticks. Set this for the quoted structure.",
        )
    with c3:
        cost_ticks = st.number_input(
            "Cost / slippage (ticks per side per lot)",
            min_value=0.0,
            value=0.0,
            step=0.1,
            key="z_cost_ticks",
        )
    with c4:
        side_mode = st.segmented_control(
            "Trade direction",
            options=["Both", "Long only", "Short only"],
            default="Both",
            key="z_side_mode",
        )

    st.markdown("**Entry, exit and stop-loss tranches**")
    st.caption(
        "For shorts, +Entry Z sells the tranche and +Exit Z buys it back on a downward crossing. "
        "Longs mirror the same levels below zero. Stop loss is a price/tick risk stop measured from "
        "that tranche's actual entry price; enter 0 to disable it."
    )

    defaults = [(1.50, 1.00, 1, 0.0), (2.00, 1.50, 1, 0.0), (2.50, 2.00, 2, 0.0)]
    specs: list[TrancheSpec] = []
    for index, (default_entry, default_exit, default_qty, default_stop) in enumerate(defaults, start=1):
        cols = st.columns([1, 1, 1, 1])
        with cols[0]:
            entry_z = st.number_input(
                f"Tranche {index} entry |Z|",
                min_value=0.05,
                max_value=20.0,
                value=default_entry,
                step=0.05,
                key=f"z_entry_{index}",
            )
        with cols[1]:
            exit_z = st.number_input(
                f"Tranche {index} exit |Z|",
                min_value=0.0,
                max_value=20.0,
                value=default_exit,
                step=0.05,
                key=f"z_exit_{index}",
            )
        with cols[2]:
            qty = st.number_input(
                f"Tranche {index} quantity",
                min_value=1,
                max_value=100,
                value=default_qty,
                step=1,
                key=f"z_qty_{index}",
            )
        with cols[3]:
            stop_ticks = st.number_input(
                f"Tranche {index} stop loss (ticks)",
                min_value=0.0,
                max_value=100000.0,
                value=default_stop,
                step=0.5,
                key=f"z_stop_{index}",
                help="Adverse price distance from this tranche's entry. 0 = disabled.",
            )
        specs.append(
            TrancheSpec(
                float(entry_z),
                float(exit_z),
                int(qty),
                None if float(stop_ticks) <= 0 else float(stop_ticks),
            )
        )

    st.caption(
        "Stop backtest rule: existing stops are checked on each following candle using OHLC high/low. "
        "If the bar opens through the stop, the open is used as the simulated fill. If a stop and a Z exit "
        "could both have occurred inside the same OHLC candle, the stop is given priority as the conservative assumption."
    )

    return StrategyConfig(
        lookback=int(lookback),
        tranches=tuple(specs),
        tick_size=float(tick_size),
        cost_ticks_per_side=float(cost_ticks),
        allow_long=side_mode in {"Both", "Long only"},
        allow_short=side_mode in {"Both", "Short only"},
    )


def render_strategy_lab(frame: pd.DataFrame, interval_api: str, instrument: str) -> StrategyConfig | None:
    st.header("Z-Score Strategy Lab")
    st.caption(
        f"Research on {instrument} using the loaded {interval_api} strategy candles. "
        "When QH has no native table, larger candles are aggregated only from observed smaller QH bars; "
        "empty intervals remain absent and nothing is forward-filled."
    )

    try:
        config = _build_strategy_config()
    except ValueError as exc:
        st.error(str(exc), icon=":material/error:")
        return None

    if frame.attrs.get("derived_dfly") and any(spec.stop_loss_ticks is not None for spec in config.tranches):
        st.error("Set stop loss to 0 for a calculated DFly. Actual DFly intrabar highs/lows are unavailable from separate fly candles.")
        return None

    if len(frame) <= config.lookback + 1:
        st.warning(
            f"Only {len(frame)} bars are loaded. A {config.lookback}-candle benchmark needs at "
            f"least {config.lookback + 2} bars before it can produce crossings. Load more QH bars.",
            icon=":material/data_alert:",
        )
        return config

    try:
        result = run_zscore_backtest(frame, config)
    except ValueError as exc:
        st.error(str(exc), icon=":material/error:")
        return config

    summary = result["summary"]
    zdata = result["data"]
    trades = result["trades"]
    events = result["events"]
    states = result["states"]
    active = result["active"]

    st.divider()
    st.subheader("Backtest summary")
    r1 = st.columns(5)
    r1[0].metric("Completed tranches", f"{summary['completed_trades']:,}")
    r1[1].metric("Net P&L", f"{summary['net_ticks']:+,.1f} ticks")
    r1[2].metric("Win rate", f"{summary['win_rate_pct']:.1f}%")
    r1[3].metric("Max drawdown", f"{summary['max_drawdown_ticks']:,.1f} ticks")
    r1[4].metric("Max abs position", f"{summary['max_abs_position']} lots")

    r2 = st.columns(5)
    r2[0].metric("Average winner", f"{summary['avg_win_ticks']:+,.1f} ticks")
    r2[1].metric("Average loser", f"{summary['avg_loss_ticks']:+,.1f} ticks")
    r2[2].metric("Open tranches", f"{summary['open_tranches']}")
    r2[3].metric("Open unrealized", f"{summary['open_unrealized_ticks']:+,.1f} ticks")
    r2[4].metric("Latest Z", _format_signed(float(summary["latest_z"])))

    r3 = st.columns(5)
    r3[0].metric("Stop-loss exits", f"{summary['stopouts']:,}")
    r3[1].metric("Stop-loss P&L", f"{summary['stopout_net_ticks']:+,.1f} ticks")
    r3[2].metric("Z-score exits", f"{summary['z_exits']:,}")
    r3[3].metric("Z-exit P&L", f"{summary['z_exit_net_ticks']:+,.1f} ticks")
    r3[4].metric("Total costs", f"{summary['total_cost_ticks']:,.1f} ticks")

    st.caption(
        "A 'completed tranche' is one independently managed scale-in unit, not an entire multi-tranche campaign. "
        "Signals and simulated fills are evaluated at completed-bar close in this first research build."
    )

    chart_left, chart_right = st.columns(2)
    with chart_left:
        st.markdown("**Rolling Z-score**")
        zchart = zdata[["strategy_time_london", "zscore"]].dropna().copy()
        if not zchart.empty:
            for index, spec in enumerate(config.tranches, start=1):
                zchart[f"short entry T{index}"] = spec.entry_z
                zchart[f"long entry T{index}"] = -spec.entry_z
            zchart = zchart.set_index("strategy_time_london")
            st.line_chart(zchart, height=360)
        else:
            st.info("No valid Z-score observations yet.")

    with chart_right:
        st.markdown("**Strategy equity (mark-to-market ticks)**")
        if not states.empty:
            equity = states[["time", "equity_ticks"]].set_index("time")
            st.line_chart(equity, height=360)
        else:
            st.info("No strategy state is available.")

    st.divider()
    st.subheader("Latest-bar / live-signal state")
    st.info(
        "This is a signal preview from the latest loaded QH OHLC bar. It does not place orders and is not yet a streaming/broker execution engine.",
        icon=":material/monitoring:",
    )

    latest_valid = zdata.dropna(subset=["zscore"])
    if latest_valid.empty:
        st.warning("The loaded history does not yet contain a valid Z-score.")
    else:
        latest = latest_valid.iloc[-1]
        latest_state = states.iloc[-1]
        state_cols = st.columns(5)
        state_cols[0].metric("Latest price", f"{float(latest['close']):.6f}")
        state_cols[1].metric("Current Z", _format_signed(float(latest["zscore"])))
        state_cols[2].metric("Rolling mean", f"{float(latest['z_mean']):.6f}")
        state_cols[3].metric("Rolling SD", f"{float(latest['z_std']):.6f}")
        state_cols[4].metric("Model position", f"{int(latest_state['position']):+d} lots")
        st.caption(
            f"Latest strategy timestamp: {latest['strategy_time_london']:%d %b %Y %H:%M %Z}. "
            "As new candles arrive, the prior-X-candle benchmark rolls and this Z changes automatically."
        )

        if active.empty:
            st.success("No tranche is currently active in the reconstructed strategy state.")
        else:
            st.markdown("**Active tranches**")
            st.dataframe(active, hide_index=True, width="stretch")

        thresholds = pd.DataFrame(next_thresholds(config, active))
        st.markdown("**Next Z actions**")
        st.dataframe(thresholds, hide_index=True, width="stretch")

    st.divider()
    tabs = st.tabs(["Completed trades", "Signal/event log", "Z-score audit"])
    with tabs[0]:
        if trades.empty:
            st.info("No completed tranches for these parameters.")
        else:
            display_trades = trades.sort_values("exit_time", ascending=False)
            st.dataframe(display_trades, hide_index=True, width="stretch")
            st.download_button(
                "Download completed trades CSV",
                data=display_trades.to_csv(index=False).encode("utf-8"),
                file_name=f"{instrument}_{interval_api}_zscore_trades.csv",
                mime="text/csv",
                icon=":material/download:",
            )
    with tabs[1]:
        if events.empty:
            st.info("No entry/exit crossings occurred.")
        else:
            st.dataframe(events.sort_values("time", ascending=False), hide_index=True, width="stretch")
    with tabs[2]:
        audit_cols = [
            "strategy_time_london",
            "close",
            "z_mean",
            "z_std",
            "zscore",
        ]
        st.dataframe(zdata[audit_cols].sort_values("strategy_time_london", ascending=False), hide_index=True, width="stretch")

    st.warning(
        "Research note: Z entries/exits are still simulated at completed-bar close. Stop losses use completed-bar OHLC high/low with a conservative stop-first assumption. Before using real money, we should add bid/ask execution, slippage, walk-forward testing, and out-of-sample validation.",
        icon=":material/science:",
    )
    return config


def _render_live_snapshot(
    frame: pd.DataFrame,
    interval_api: str,
    instrument: str,
    config: StrategyConfig | None,
    *,
    signal_timing: str,
) -> None:
    hub = get_live_hub()
    snap = hub.snapshot()
    status = str(snap.get("status") or "DISCONNECTED")
    resolution = snap.get("resolution")
    structure = snap.get("structure")

    status_cols = st.columns(4)
    status_cols[0].metric("Lightstreamer status", status)
    status_cols[1].metric("Last update", _format_age(snap.get("last_update_utc")))
    status_cols[2].metric("Live ticks stored", f"{len(hub.ticks_frame()):,}")
    status_cols[3].metric("Price source", str(snap.get("price_source") or "—"))

    if snap.get("error"):
        st.error(str(snap["error"]), icon=":material/error:")

    if resolution is None:
        st.info("Use **Connect live feed** in the sidebar to start Lightstreamer.", icon=":material/cable:")
        return

    st.caption(
        f"Live legs: {', '.join(resolution.qh_leg_codes)} · TT items: {', '.join(resolution.item_names)} · "
        f"formula: {resolution.definition.expression}"
    )
    st.warning(
        "The Lightstreamer code you supplied discovers **OUT** contracts. Therefore the live structure shown here is "
        "an indicative/synthetic structure calculated from those live outright legs. Historical backtests remain on the "
        "direct QH quoted structure. Do not treat the synthetic live bid/ask as a directly quoted exchange structure price.",
        icon=":material/info:",
    )

    if not structure or structure.get("price") is None:
        st.info("Connected, but waiting until all required live legs have usable prices.")
    else:
        raw_live_price = float(structure["price"])
        bid = structure.get("bid")
        ask = structure.get("ask")
        ticks = hub.ticks_frame()
        try:
            live_completed, developing = build_live_candles(ticks, interval_api)
        except LiveFeedError as exc:
            st.error(str(exc))
            live_completed = pd.DataFrame()
            developing = pd.DataFrame()
        combined = append_live_candles_to_history(frame, live_completed, instrument)

        # The live Z is explicitly the current close of the developing selected-timeframe
        # candle. It may update every second, but it is NOT treated as a new 1-second bar.
        # The benchmark stays frozen until this selected candle completes.
        if not developing.empty:
            live_price = float(developing.iloc[-1]["close"])
        else:
            live_price = raw_live_price

        mean = std = live_z = float("nan")
        if config is not None:
            mean, std, live_z = _live_zscore(combined, config.lookback, live_price)

        quote_cols = st.columns(6)
        quote_cols[0].metric(f"LIVE structure · {interval_api} close", f"{live_price:.6f}")
        quote_cols[1].metric("Synthetic bid", "—" if bid is None else f"{float(bid):.6f}")
        quote_cols[2].metric("Synthetic ask", "—" if ask is None else f"{float(ask):.6f}")
        quote_cols[3].metric(f"LIVE Z · developing {interval_api}", _format_signed(live_z))
        quote_cols[4].metric("Frozen benchmark mean", "—" if not np.isfinite(mean) else f"{mean:.6f}")
        quote_cols[5].metric("Frozen benchmark SD", "—" if not np.isfinite(std) else f"{std:.6f}")

        st.caption(
            f"Time-aligned logic: with {interval_api} selected, LIVE Z is the changing close of the current "
            f"{interval_api} candle measured against the previous X completed {interval_api} candles. "
            "The mean/SD benchmark is locked during the developing candle and changes only when that candle closes."
        )

        if not developing.empty:
            d = developing.iloc[-1]
            clock = _developing_candle_clock(developing, interval_api)
            st.markdown(f"**Developing {interval_api} candle**")
            dc = st.columns(7)
            if clock is not None:
                dc[0].metric("Candle start", clock["start_london"].strftime("%H:%M:%S"))
                dc[1].metric("Candle closes", clock["end_london"].strftime("%H:%M:%S"))
                dc[2].metric("Time left", _format_countdown(clock["seconds_left"]))
            else:
                dc[0].metric("Candle start", "—")
                dc[1].metric("Candle closes", "—")
                dc[2].metric("Time left", "—")
            dc[3].metric("Open", f"{float(d['open']):.6f}")
            dc[4].metric("High", f"{float(d['high']):.6f}")
            dc[5].metric("Low", f"{float(d['low']):.6f}")
            dc[6].metric("Current close", f"{float(d['close']):.6f}")
            st.caption(f"Observed live structure updates in this {interval_api} candle: {int(d['live_tick_count']):,}")

        if config is not None and len(combined) > config.lookback + 1:
            try:
                live_result = run_zscore_backtest(combined, config)
            except ValueError as exc:
                st.error(f"Could not reconstruct live strategy state: {exc}")
            else:
                summary = live_result["summary"]
                active = live_result["active"]
                state_frame = live_result["states"]
                current_position = int(state_frame.iloc[-1]["position"]) if not state_frame.empty else 0
                last_completed_z = float(summary["latest_z"])

                if signal_timing == "Intrabar live":
                    st.markdown(f"**LIVE {interval_api} entry / exit signal**")

                    config_signature = (
                        instrument,
                        interval_api,
                        config.lookback,
                        config.tick_size,
                        config.allow_long,
                        config.allow_short,
                        tuple(
                            (spec.entry_z, spec.exit_z, spec.qty, spec.stop_loss_ticks)
                            for spec in config.tranches
                        ),
                        tuple(resolution.item_names),
                    )
                    tracker = st.session_state.get("_live_intrabar_z_tracker")
                    if not isinstance(tracker, dict) or tracker.get("signature") != config_signature:
                        tracker = seed_live_intrabar_state(
                            config, active, previous_z=last_completed_z
                        )
                        tracker["signature"] = config_signature
                        tracker["last_sample_at"] = None
                        st.session_state["_live_intrabar_z_tracker"] = tracker

                    reset_key = f"reset_live_intrabar_{instrument}_{interval_api}"
                    if st.button(
                        "Reset live signal state to completed model",
                        key=reset_key,
                        help=(
                            "Use this after reconnecting or if you did not act on earlier intrabar signals. "
                            "It re-seeds the live signal state from the last completed-candle model position."
                        ),
                    ):
                        tracker = seed_live_intrabar_state(
                            config, active, previous_z=last_completed_z
                        )
                        tracker["signature"] = config_signature
                        tracker["last_sample_at"] = None
                        st.session_state["_live_intrabar_z_tracker"] = tracker

                    # Process at most one Z observation per displayed live refresh. This avoids
                    # treating asynchronous individual leg updates as separate strategy bars.
                    sample_at = snap.get("last_update_utc")
                    sample_token = None if sample_at is None else str(pd.Timestamp(sample_at).value)
                    new_events: list[dict[str, object]] = []
                    if sample_token is not None and tracker.get("last_sample_at") != sample_token:
                        tracker, new_events = step_live_intrabar_state(
                            tracker,
                            config,
                            current_z=live_z,
                            current_price=live_price,
                            timestamp=pd.Timestamp(sample_at),
                        )
                        tracker["signature"] = config_signature
                        tracker["last_sample_at"] = sample_token
                        st.session_state["_live_intrabar_z_tracker"] = tracker

                    live_active = live_intrabar_active_frame(tracker)
                    live_position = 0
                    if not live_active.empty:
                        long_qty = int(live_active.loc[live_active["side"] == "LONG", "qty"].sum())
                        short_qty = int(live_active.loc[live_active["side"] == "SHORT", "qty"].sum())
                        live_position = long_qty - short_qty

                    if new_events:
                        newest = new_events[-1]
                        st.success(
                            f"NEW LIVE SIGNAL: **{newest['action']} T{newest['tranche']} x{newest['qty']}** "
                            f"at Z {float(newest['zscore']):+.2f}, price {float(newest['price']):.6f}."
                        )

                    event_history = tracker.get("events", [])
                    if isinstance(event_history, list) and event_history:
                        last_event = event_history[-1]
                        st.info(
                            f"Last intrabar signal: **{last_event['action']} T{last_event['tranche']} x{last_event['qty']}** · "
                            f"Z {float(last_event['zscore']):+.2f} · price {float(last_event['price']):.6f} · "
                            f"{pd.Timestamp(last_event['time']).tz_convert('Europe/London').strftime('%H:%M:%S')} London."
                        )

                    live_rows = live_threshold_status(
                        config,
                        live_active,
                        current_z=live_z,
                        last_completed_z=last_completed_z,
                        mean=mean,
                        std=std,
                    )
                    live_actions = [row for row in live_rows if bool(row["condition_met"])]
                    if live_actions:
                        label = " · ".join(
                            f"{row['action']} T{row['tranche']} x{row['qty']}"
                            for row in live_actions
                        )
                        st.warning(
                            f"ACTIONABLE AT CURRENT Z: **{label}**. This is the next live action from the intrabar signal state."
                        )
                    else:
                        waiting = sorted(live_rows, key=lambda row: float(row["distance_z"]))
                        if waiting:
                            nearest = waiting[0]
                            price_text = (
                                "—"
                                if not np.isfinite(float(nearest["trigger_price"]))
                                else f"{float(nearest['trigger_price']):.6f}"
                            )
                            st.info(
                                f"NO NEW ACTION NOW. Nearest next threshold: {nearest['action']} T{nearest['tranche']} "
                                f"at Z {float(nearest['trigger_z']):+.2f} / structure price {price_text} "
                                f"({float(nearest['distance_z']):.2f} Z away)."
                            )

                    lpos = st.columns(3)
                    lpos[0].metric("Intrabar signal position", f"{live_position:+d} lots")
                    lpos[1].metric("Live active tranches", f"{len(live_active):,}")
                    lpos[2].metric("Live signal events", f"{len(event_history) if isinstance(event_history, list) else 0:,}")
                    if not live_active.empty:
                        with st.expander("Intrabar active signal tranches", expanded=True):
                            st.dataframe(live_active, hide_index=True, width="stretch")

                    live_table = pd.DataFrame(live_rows)
                    if not live_table.empty:
                        live_table["status"] = np.where(
                            live_table["condition_met"], "ACTION NOW", "WAIT"
                        )
                        display_cols = [
                            "status", "side", "tranche", "action", "current_z",
                            "trigger_z", "trigger_price", "distance_z", "qty",
                        ]
                        st.dataframe(live_table[display_cols], hide_index=True, width="stretch")

                    if isinstance(event_history, list) and event_history:
                        with st.expander("Recent intrabar BUY / SELL signals"):
                            st.dataframe(
                                pd.DataFrame(event_history[-50:]).sort_values("time", ascending=False),
                                hide_index=True,
                                width="stretch",
                            )
                    st.caption(
                        f"Intrabar mode uses one displayed live-Z observation at a time inside the current {interval_api} candle. "
                        f"It tracks its own signal state, so a live entry can later produce a live exit inside the same {interval_api} candle. "
                        "This intrabar state is separate from the completed-candle backtest and does not send orders."
                    )
                else:
                    st.info(
                        f"Signal timing is set to **Candle close only**. LIVE Z still moves inside the {interval_api} candle, "
                        f"but BUY/SELL state changes are intentionally delayed until the {interval_api} candle completes."
                    )

                st.markdown("**Completed-candle strategy state**")
                lc = st.columns(4)
                lc[0].metric("Model position", f"{current_position:+d} lots")
                lc[1].metric("Completed live candles", f"{len(live_completed):,}")
                lc[2].metric("Open tranches", f"{summary['open_tranches']}")
                lc[3].metric(f"Last completed {interval_api} Z", _format_signed(last_completed_z))
                if not active.empty:
                    st.dataframe(active, hide_index=True, width="stretch")
                st.markdown("**Next Z actions / equivalent structure prices**")
                st.dataframe(
                    pd.DataFrame(next_thresholds(config, active, mean=mean, std=std)),
                    hide_index=True,
                    width="stretch",
                )

        if not live_completed.empty:
            with st.expander("Completed Lightstreamer candles"):
                st.dataframe(
                    live_completed.sort_values("strategy_time_london", ascending=False).head(100),
                    hide_index=True,
                    width="stretch",
                )

    quotes = snap.get("quotes") or {}
    if quotes:
        leg_rows = []
        for leg_code, item in zip(resolution.qh_leg_codes, resolution.item_names):
            q = quotes.get(item, {})
            leg_rows.append(
                {
                    "leg": leg_code,
                    "TT item": item,
                    "AdminPrice": q.get("AdminPrice"),
                    "BidPrice": q.get("BidPrice"),
                    "BidSize": q.get("BidSize"),
                    "AskPrice": q.get("AskPrice"),
                    "AskSize": q.get("AskSize"),
                    "LastPrice": q.get("LastPrice"),
                    "LastSize": q.get("LastSize"),
                    "MP": q.get("MP"),
                    "WAP": q.get("WAP"),
                    "TotalTradedVolume": q.get("TotalTradedVolume"),
                    "Timestamp": q.get("Timestamp"),
                }
            )
        st.markdown("**Live outright legs**")
        st.dataframe(pd.DataFrame(leg_rows), hide_index=True, width="stretch")


def render_live_feed(
    frame: pd.DataFrame,
    interval_api: str,
    instrument: str,
    config: StrategyConfig | None,
    *,
    auto_refresh: bool,
    signal_timing: str,
) -> None:
    st.header("Live Lightstreamer")
    st.caption(
        "Historical bars still come from QH. Live updates come from ls-md / AdminPriceAdapter using the "
        "Insight X OUT-contract mapping you supplied. This build is market-data/signal only and does not send orders."
    )
    connected = get_live_hub().snapshot().get("resolution") is not None
    run_every = "1s" if auto_refresh and connected else None

    @st.fragment(run_every=run_every)
    def _live_fragment() -> None:
        _render_live_snapshot(
            frame, interval_api, instrument, config, signal_timing=signal_timing
        )

    _live_fragment()


st.title("Screner")
st.caption(
    "Direct QH structure charts, backtests and live OHLC polling, plus Insight file prices. No candles are forward-filled."
)

qh_token_ready = True
live_coefficients = ""
live_price_source = "AdminPrice"
live_auto_refresh = False
live_signal_timing = "Intrabar live"
with st.sidebar:
    st.header("Data controls")
    source = st.segmented_control(
        "Data source",
        options=["Sample", "Upload", "QH API", "QH API + Live", "Insight file (polling)"],
        default="Sample",
        key="data_source",
    )
    instrument = st.text_input(
        "Quoted instrument / structure",
        value="COF27-G27-H27",
        help="Enter the QH structure code. An unavailable four-contract DFly is calculated as the difference of two directly quoted flies.",
    ).strip()
    insight_url = DEFAULT_INSIGHT_URL
    insight_payload = None
    insight_poll_seconds = 15
    insight_stale_seconds = 180
    insight_missing = -100.0
    insight_timezone = "UTC"
    if source == "Insight file (polling)":
        insight_url = st.text_input("Insight price-file URL", value=DEFAULT_INSIGHT_URL)
        insight_poll_seconds = int(st.number_input("Poll every (seconds)", min_value=5, max_value=300, value=15))
        insight_stale_seconds = int(st.number_input("Maximum price age (seconds)", min_value=10, max_value=3600, value=180))
        exclude_missing = st.checkbox("Treat -100 as missing", value=True,
            help="The screenshot repeatedly contains -100. Disable this only if -100 is a real price in your source.")
        insight_missing = -100.0 if exclude_missing else None
        insight_timezone = st.selectbox("Timezone for timestamps without Z / offset", ["UTC", "Europe/London"])
        live_auto_refresh = st.toggle("Poll Insight prices automatically", value=True)
        live_signal_timing = st.segmented_control("Insight signal timing",
            options=["Intrabar live", "Candle close only"], default="Intrabar live")
        st.caption("Select the exact instrument name used in this file. No CO/LCO symbol substitution is assumed.")
        try:
            insight_payload = fetch_insight(insight_url)
        except InsightPriceError as exc:
            st.error(str(exc))
            st.info("Open this app on a machine with access to the corporate URL. A browser login may not also authenticate Python requests.")
            st.stop()
        insight_names = insight_payload[0]
        instrument = st.selectbox("Insight instrument", insight_names,
            index=insight_names.index(instrument) if instrument in insight_names else 0)
    interval_choice = st.selectbox(
        "Candle interval",
        options=INTERVAL_PRESETS,
        index=1,
        help=(
            "This is also the strategy candle interval N. If QH has no native table (for example 15M), "
            "the app loads real smaller QH bars and aggregates them locally without filling empty intervals."
        ),
    )
    if interval_choice == "Custom":
        interval_api = st.text_input(
            "Custom QH interval",
            value="",
            placeholder="e.g. 2H",
            help="Enter the interval value exactly as accepted by the QH API.",
        ).strip().upper()
        if not interval_api:
            st.info("Enter a custom QH interval value to continue.")
            st.stop()
    else:
        interval_api = interval_choice
    interval = interval_api.lower()
    offset_minutes = st.number_input(
        "Source timestamp correction (minutes)",
        min_value=-720,
        max_value=720,
        value=0 if source == "Insight file (polling)" else 65,
        step=5,
        help=(
            "The correction applied is strategy time = raw API time − offset. "
            "Set this to 0 if the backend timestamps are already correct."
        ),
    )

    uploaded_file = None
    count = 500
    if source == "Upload":
        uploaded_file = st.file_uploader("Upload QH JSON", type=["json"])
    elif source in {"QH API", "QH API + Live"}:
        count = st.number_input(
            "Maximum bars",
            min_value=1,
            max_value=10_000,
            value=500,
            step=100,
            help="For strategy research, load enough bars to cover the lookback and many independent trades.",
        )
        if "qh_refresh_key" not in st.session_state:
            st.session_state.qh_refresh_key = 0
        if st.button("Refresh QH bars now", icon=":material/refresh:", use_container_width=True):
            st.session_state.qh_refresh_key += 1
        st.caption("Refreshes the exact structure from QH. Rate limits and retry/backoff still apply.")
        st.divider()
        qh_token_ready = render_qh_access_panel()

        st.divider()
        st.header("QH auto refresh")
        qh_poll_seconds = int(st.number_input("QH polling interval (seconds)", min_value=5, max_value=300, value=5))
        qh_stale_seconds = int(st.number_input("Maximum delay after candle end (seconds)", min_value=30, max_value=3600, value=180))
        live_auto_refresh = st.toggle("Auto refresh QH charts and results", value=True)
        st.caption("OHLC limit: 30 requests/minute. The 5-second default normally needs 12 requests/minute. DFly components are fetched together in one request.")
        live_signal_timing = st.segmented_control("Signal timing",
            options=["Candle close only", "Intrabar live"], default="Candle close only",
            help="Intrabar mode requires QH to return a developing candle. Charts show it while backtests use completed candles.")

    st.header("Chart controls")
    chart_type = st.segmented_control(
        "Price chart", options=["Candlestick", "Line"], default="Candlestick"
    )
    show_volume = st.toggle("Show volume", value=True)
    navigation_mode = st.segmented_control(
        "Chart navigation",
        options=["Time zoom", "Free pan", "Page scroll"],
        default="Time zoom",
        help=(
            "Time zoom: wheel zooms the time axis and drag pans sideways. "
            "Free pan: drag the chart sideways/up-down while the wheel scrolls the page. "
            "Page scroll: chart dragging and wheel zoom are disabled."
        ),
    )
    compress_gaps = st.toggle(
        "Compress non-trading gaps",
        value=True,
        help=(
            "When on, missing/non-trading intervals take no horizontal space. "
            "No missing candle is ever created."
        ),
    )
    chart_theme = st.segmented_control(
        "Chart theme",
        options=["Dark", "Light"],
        default="Dark",
    )


def _render_dashboard():

    if not instrument:
        st.warning("Enter a directly quoted instrument code.", icon=":material/warning:")
        st.stop()

    if source in {"QH API", "QH API + Live"} and not qh_token_ready:
        st.info(
            "QH API access needs a token. Use **QH access** in the sidebar: open Microsoft sign-in, "
            "copy the returned access/JSON, paste it, and click **Save & use access token**.",
            icon=":material/key:",
        )
        st.stop()

    if source in {"QH API", "QH API + Live"}:
        st.caption(f"Last refresh: {pd.Timestamp.now(tz='Europe/London'):%d %b %Y %H:%M:%S %Z} · Auto refresh: {'ON' if live_auto_refresh else 'OFF'} · Interval: {qh_poll_seconds}s. Charts include a developing candle when QH supplies one; backtests use completed candles.")

    try:
        api_meta: dict[str, str | None] = {}
        from_cache = False
        qh_source_interval = interval_api
        if source == "Insight file (polling)":
            with st.spinner("Reading the selected instrument from Insight history…"):
                insight_payload = fetch_insight(insight_url, instrument)
            insight_snapshot = build_insight_snapshot(*insight_payload, instrument, interval_api,
                missing_value=insight_missing, naive_timezone=insight_timezone, offset_minutes=int(offset_minutes))
            frame = insight_snapshot.completed
            st.info("Insight candles contain observed file prices, not exchange OHLC extremes; volume is unavailable. The chart and backtest use completed candles only. Live signals require fresh source timestamps.")
        elif source == "Sample":
            raw_payload = load_json_file(str(SAMPLE_FILE))
        elif source == "Upload":
            if uploaded_file is None:
                st.info("Choose a JSON file to begin.", icon=":material/upload_file:")
                st.stop()
            raw_payload = json.loads(uploaded_file.getvalue().decode("utf-8-sig"))
        else:
            with st.spinner("Fetching direct structure OHLC from QH…"):
                qh_client = QHClient(auth_file=AUTH_FILE, cache_dir=APP_DIR / ".cache")
                frame, api_meta, qh_source_interval = fetch_direct_structure(
                    qh_client, instrument, interval_api, int(count), int(offset_minutes),
                    cache_ttl_seconds=0 if st.session_state.get("qh_refresh_key", 0) else 5)
                qh_chart_frame = frame.copy()
                frame, _, _, _ = split_direct_candles(frame, interval_api)

        if source in {"Sample", "Upload"}:
            frame = prepare_ohlc_data(raw_payload,
                timestamp_offset_minutes=int(offset_minutes), instrument=instrument)

    except QHAuthenticationError as exc:
        st.error(str(exc), icon=":material/key:")
        st.info(
            "Use **QH access** in the sidebar to open Microsoft sign-in and save the new access response."
        )
        st.stop()
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        st.error(f"Could not read the JSON data: {exc}", icon=":material/error:")
        st.stop()
    except (DataValidationError, QHAPIError, ValueError) as exc:
        st.error(str(exc), icon=":material/error:")
        st.stop()

    if frame.empty:
        st.warning(
            f"No bars were found for {instrument}. The app did not substitute or synthesize data.",
            icon=":material/warning:",
        )
        st.stop()

    chart_frame = qh_chart_frame if source in {"QH API", "QH API + Live"} else frame
    gaps = summarize_gaps(chart_frame, interval)
    if frame.attrs.get("derived_dfly"):
        st.info(frame.attrs["source_note"])
    first_time = chart_frame["strategy_time_london"].iloc[0]
    last_time = chart_frame["strategy_time_london"].iloc[-1]

    with st.container(horizontal=True):
        st.metric("Quoted structure", frame["product"].iloc[0], border=True)
        st.metric("Bars", f"{len(frame):,}", border=True)
        st.metric("Total volume", "Unavailable" if frame.attrs.get("derived_dfly") else f"{frame['volume'].sum():,.0f}", border=True)
        st.metric("Missing intervals", f"{gaps.missing_intervals:,}", border=True)

    main_tabs = st.tabs(["Price / OHLC", "Z-Score Strategy Lab", "Live prices", "Data audit"])

    with main_tabs[0]:
        with st.container(border=True):
            heading = f"{instrument} · {interval_api} " + ("calculated DFly endpoints" if frame.attrs.get("derived_dfly") else "quoted OHLC")
            st.subheader(heading)
            render_chart_zscore(chart_frame, interval_api,
                source_key=(source, instrument, int(offset_minutes)),
                stale_seconds=qh_stale_seconds if source in {"QH API", "QH API + Live"} else 180,
                live_source=source in {"QH API", "QH API + Live"})
            if source in {"QH API", "QH API + Live"} and qh_source_interval.upper() != interval_api.upper():
                st.info(
                    f"QH does not expose this strategy interval as the selected source table. "
                    f"Loaded real {qh_source_interval} bars and aggregated them to {interval_api} locally. "
                    "Empty buckets are omitted; no candle is forward-filled.",
                    icon=":material/merge_type:",
                )
            st.caption(
                f"Corrected strategy timestamps: {first_time:%d %b %Y %H:%M %Z} → "
                f"{last_time:%d %b %Y %H:%M %Z} · offset −{int(offset_minutes)} minutes"
            )
            render_ohlc_chart(
                chart_frame,
                chart_type=chart_type.lower(),
                show_volume=show_volume and not frame.attrs.get("derived_dfly", False),
                compress_gaps=compress_gaps,
                interval=interval,
                navigation_mode=navigation_mode,
                height=720 if show_volume else 590,
                chart_theme=chart_theme or "Dark",
                key="quoted_ohlc_chart",
            )
            if navigation_mode == "Time zoom":
                st.caption(
                    "TradingView controls: wheel = horizontal candle zoom · drag chart = sideways pan · "
                    "drag the right price scale = stretch/compress Y · double-click the price scale = autoscale"
                )
            elif navigation_mode == "Free pan":
                st.caption(
                    "Free pan: drag chart = sideways pan · wheel/touchpad = page scroll · "
                    "drag the right price scale = stretch/compress Y · double-click the price scale = autoscale"
                )
            else:
                st.caption(
                    "Page scroll: wheel/touchpad scrolls the page · chart drag/zoom is disabled."
                )

        if gaps.missing_intervals:
            st.warning(
                f"Detected {gaps.missing_intervals} absent {interval_api} interval(s) across "
                f"{gaps.gap_count} gap(s). They remain absent—no candles were created or forward-filled.",
                icon=":material/data_alert:",
            )

    with main_tabs[1]:
        strategy_config = render_strategy_lab(frame, interval_api, instrument)

    with main_tabs[2]:
        if source == "Insight file (polling)":
            render_insight_panel(insight_url, instrument, interval_api, strategy_config,
                polling_seconds=insight_poll_seconds, stale_seconds=insight_stale_seconds,
                missing_value=insight_missing, naive_timezone=insight_timezone,
                offset_minutes=int(offset_minutes), signal_timing=live_signal_timing,
                auto_refresh=bool(live_auto_refresh))
        elif source not in {"QH API", "QH API + Live"}:
            st.info(
                "Choose **QH API + Live** for direct structure OHLC or **Insight file (polling)** for the supplied file URL.",
                icon=":material/cable:",
            )
        else:
            render_qh_direct_panel(APP_DIR, instrument, interval_api, int(count), strategy_config,
                offset_minutes=int(offset_minutes), polling_seconds=qh_poll_seconds,
                stale_seconds=qh_stale_seconds, auto_refresh=False,
                signal_timing=str(live_signal_timing))

    with main_tabs[3]:
        if source in {"QH API", "QH API + Live"}:
            cache_label = "disk/Streamlit cache" if from_cache else "QH API"
            source_detail = (
                f"{qh_source_interval} QH bars → local {interval_api} aggregation"
                if qh_source_interval.upper() != interval_api.upper()
                else f"native {qh_source_interval} QH bars"
            )
            st.caption(
                f"Loaded from {cache_label} ({source_detail}). Rate limit: "
                f"{api_meta.get('remaining') or '—'} remaining of {api_meta.get('limit') or '—'}; "
                f"reset {api_meta.get('reset') or '—'}."
            )

        st.subheader("Timestamp audit and raw bars")
        st.caption(
            "raw_timestamp_ms is preserved unchanged. strategy_time_london is the corrected timestamp "
            "used by both the chart and Z-score strategy logic."
        )
        display = chart_frame[
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
        ].sort_values("strategy_time_london", ascending=False)
        st.dataframe(display, hide_index=True, width="stretch")


if source in {"QH API", "QH API + Live"} and live_auto_refresh:
    st.fragment(run_every=qh_poll_seconds)(_render_dashboard)()
else:
    _render_dashboard()
