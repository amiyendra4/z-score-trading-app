"""Polling UI for observed Insight prices; never submits orders."""
import numpy as np
import pandas as pd
import streamlit as st

from insight_prices import InsightPriceError, fetch_insight, build_insight_snapshot
from strategy_engine import run_zscore_backtest, seed_live_intrabar_state, step_live_intrabar_state, live_intrabar_active_frame, next_thresholds


def render_insight_panel(url, instrument, interval, config, *, polling_seconds=15,
                         stale_seconds=180, missing_value=-100.0, naive_timezone="UTC",
                         offset_minutes=0, signal_timing="Intrabar live", auto_refresh=True):
    @st.fragment(run_every=polling_seconds if auto_refresh else None)
    def panel():
        st.header("Insight file prices")
        st.caption("Polls the file for the selected quoted instrument. Prices are observations; no bid/ask or traded volume is supplied. No orders are sent.")
        st.button("Refresh Insight prices now", key="insight_panel_refresh")
        try:
            names, rows = fetch_insight(url, instrument)
            snap = build_insight_snapshot(names, rows, instrument, interval,
                missing_value=missing_value, naive_timezone=naive_timezone, offset_minutes=offset_minutes)
        except (InsightPriceError, ValueError) as exc:
            st.error(str(exc))
            st.info("Price refresh failed. No new signals are generated.")
            return
        columns = st.columns(4)
        columns[0].metric("Latest observed price", f"{snap.latest_price:.6f}")
        columns[1].metric("Latest price time", snap.latest_time.tz_convert("Europe/London").strftime("%d %b %Y %H:%M:%S %Z"))
        columns[2].metric("Price age", f"{snap.age_seconds / 60:.1f} minutes")
        fresh = -5 <= snap.age_seconds <= stale_seconds
        columns[3].metric("Freshness", "FRESH" if fresh else "STALE / FUTURE")
        st.caption(f"Checked {pd.Timestamp.now(tz='Europe/London'):%d %b %Y %H:%M:%S %Z}. {snap.skipped:,} missing/invalid observations omitted. Polling every {polling_seconds}s does not make the source update faster.")
        if not fresh:
            st.warning("This file does not currently contain a fresh price for this instrument. New live signals are blocked. Historical charts remain available.")
        if config is None or len(snap.completed) <= config.lookback:
            st.info("More completed candles are needed for the configured Z-score lookback.")
            return
        result = run_zscore_backtest(snap.completed, config)
        st.markdown("**Completed-candle model**")
        st.dataframe(result["active"], hide_index=True, use_container_width=True)
        if signal_timing == "Candle close only":
            st.caption("Entries and exits use completed observation candles. The developing candle is excluded.")
            st.dataframe(result["events"].tail(10), hide_index=True, use_container_width=True)
            return
        if snap.developing.empty:
            st.info("There is no observed price in the currently developing candle.")
            return
        candle = snap.developing.iloc[-1]
        signature = (url, instrument, interval, config, missing_value, naive_timezone, offset_minutes)
        tracker = st.session_state.get("_insight_intrabar_tracker")
        if not isinstance(tracker, dict) or tracker.get("signature") != signature:
            tracker = seed_live_intrabar_state(config, result["active"], previous_z=result["summary"]["latest_z"])
            tracker.update(signature=signature, last_timestamp=None, benchmark_bucket=None)
        if st.button("Reset Insight signal state to completed model"):
            tracker = seed_live_intrabar_state(config, result["active"], previous_z=result["summary"]["latest_z"])
            tracker.update(signature=signature, last_timestamp=None, benchmark_bucket=None)
        bucket = str(candle["strategy_time_london"])
        if tracker.get("benchmark_bucket") != bucket:
            benchmark = snap.completed["close"].iloc[-config.lookback:]
            tracker.update(benchmark_bucket=bucket, mean=float(benchmark.mean()), std=float(benchmark.std(ddof=0)))
        mean, std = tracker["mean"], tracker["std"]
        z = (snap.latest_price - mean) / std if np.isfinite(std) and std > 0 else float("nan")
        metrics = st.columns(3)
        metrics[0].metric(f"Developing {interval} Z", f"{z:+.3f}" if np.isfinite(z) else "Unavailable")
        metrics[1].metric("Frozen benchmark mean", f"{mean:.6f}")
        metrics[2].metric("Frozen benchmark SD", f"{std:.6f}")
        if fresh and np.isfinite(z):
            # Never replay a stale timestamp or silently interpret a file revision as a new tick.
            timestamp = snap.latest_time.value
            if tracker.get("last_timestamp") is None or timestamp > tracker["last_timestamp"]:
                tracker, events = step_live_intrabar_state(tracker, config, current_z=z,
                    current_price=snap.latest_price, timestamp=snap.latest_time)
                tracker["last_timestamp"] = timestamp
                if events:
                    st.success("New observed-price signal: " + ", ".join(f"{e['action']} T{e['tranche']} ×{e['qty']}" for e in events))
        st.session_state["_insight_intrabar_tracker"] = tracker
        active = live_intrabar_active_frame(tracker)
        st.markdown("**Intrabar model state and next thresholds**")
        st.dataframe(active, hide_index=True, use_container_width=True)
        st.dataframe(next_thresholds(config, active, mean=mean, std=std), hide_index=True, use_container_width=True)
        st.caption("The intrabar tracker follows sampled Z crossings. It stores stop levels but does not generate live stop-loss exits. Polling can miss crossings between file observations.")
        history = tracker.get("events", [])
        if history:
            st.dataframe(pd.DataFrame(history).tail(20), hide_index=True, use_container_width=True)
    panel()
