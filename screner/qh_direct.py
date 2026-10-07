"""Fetch quoted structures, deriving an unavailable DFly from two quoted flies."""
from pathlib import Path
import re
import time
import pandas as pd

from data_utils import DataValidationError, prepare_ohlc_data, resample_ohlc_data, interval_to_timedelta
from qh_client import QHClient, QHAPIError

_DFLY_MISSING = {}


def dfly_components(instrument):
    match = re.fullmatch(r"([A-Z]+)([FGHJKMNQUVXZ]\d{1,2})-([FGHJKMNQUVXZ]\d{1,2})-([FGHJKMNQUVXZ]\d{1,2})-([FGHJKMNQUVXZ]\d{1,2})", instrument.strip().upper())
    if not match:
        return None
    product, a, b, c, d = match.groups()
    return f"{product}{a}-{b}-{c}", f"{product}{b}-{c}-{d}"


def derive_dfly_prices(frame, instrument, components):
    first, second = components
    left = frame.loc[frame["product"].str.upper() == first].drop_duplicates("raw_timestamp_ms", keep="last")
    right = frame.loc[frame["product"].str.upper() == second].drop_duplicates("raw_timestamp_ms", keep="last")
    joined = left.merge(right, on="raw_timestamp_ms", how="inner", suffixes=("_a", "_b"))
    if joined.empty:
        raise QHAPIError(f"No matching timestamps for quoted flies {first} and {second}. No prices are filled across gaps.")
    out = pd.DataFrame({"product": instrument, "raw_timestamp_ms": joined["raw_timestamp_ms"]})
    for field in ("raw_time_utc", "raw_time_london", "strategy_time_london"):
        out[field] = joined[field + "_a"]
    out["open"] = joined["open_a"] - joined["open_b"]
    out["close"] = joined["close_a"] - joined["close_b"]
    # Separate OHLC bars cannot reconstruct the difference's intrabar extremes.
    # Only endpoint values are represented; never subtract independent highs/lows.
    out["high"] = out[["open", "close"]].max(axis=1)
    out["low"] = out[["open", "close"]].min(axis=1)
    out["volume"] = 0
    out = out.sort_values("strategy_time_london").reset_index(drop=True)
    out.attrs.update(derived_dfly=True, components=components,
        source_note=f"Calculated DFly: {first} − {second}. Uses matching QH fly-candle timestamps. High/low show endpoint ranges only; actual DFly intrabar extremes and traded volume are unavailable.")
    return out


def fetch_direct_structure(client, instrument, interval, count, offset_minutes=65, cache_ttl_seconds=5):
    if not instrument.strip() or "," in instrument:
        raise QHAPIError("Enter one exact quoted QH structure code.")
    native = {"15M": "5M", "30M": "5M", "4H": "1H"}.get(interval.upper(), interval.upper())
    duration = interval_to_timedelta(interval)
    base = interval_to_timedelta(native)
    if duration is None or base is None:
        raise QHAPIError("Choose a supported candle interval.")
    factor = max(1, int(duration / base))
    request_count = min(10_000, int(count) * factor)
    components = dfly_components(instrument)
    negative_key = (str(getattr(client, "base_url", "QH")), instrument.upper(), native)
    missing_cached = components and time.monotonic() < _DFLY_MISSING.get(negative_key, 0)
    frame = None
    if not missing_cached:
        result = client.fetch_ohlc(instruments=instrument, interval=native,
            count=request_count, cache_ttl_seconds=cache_ttl_seconds)
        try:
            frame = prepare_ohlc_data(result.payload, timestamp_offset_minutes=offset_minutes, instrument=instrument)
        except DataValidationError as exc:
            if not components or "does not contain recognizable QH OHLC bars" not in str(exc):
                raise
            if len(_DFLY_MISSING) >= 128:
                _DFLY_MISSING.clear()
            _DFLY_MISSING[negative_key] = time.monotonic() + 60
    if frame is None:
        result = client.fetch_ohlc(instruments=",".join(components), interval=native,
            count=request_count, cache_ttl_seconds=cache_ttl_seconds)
        fly_frame = prepare_ohlc_data(result.payload, timestamp_offset_minutes=offset_minutes)
        frame = derive_dfly_prices(fly_frame, instrument, components)
    else:
        exact = frame["product"].astype(str).str.casefold() == instrument.casefold()
        frame = frame.loc[exact].copy()
        if frame.empty:
            raise QHAPIError(f"QH returned no bars labelled {instrument!r}. No outright fallback is used.")
    frame = frame.drop_duplicates(["product", "strategy_time_london"], keep="last")
    metadata = frame.attrs.copy()
    if native != interval.upper():
        frame = resample_ohlc_data(frame, interval)
        frame.attrs.update(metadata)
    return frame.tail(int(count)).reset_index(drop=True), result.rate_limit, native


def split_direct_candles(frame, interval, now=None):
    current = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now).tz_convert("UTC")
    duration = interval_to_timedelta(interval)
    if duration is None:
        raise QHAPIError("Unsupported candle interval.")
    starts = frame["strategy_time_london"].dt.tz_convert("UTC")
    completed = frame.loc[starts + duration <= current].reset_index(drop=True)
    developing = frame.loc[(starts <= current) & (starts + duration > current)].reset_index(drop=True)
    latest_start = starts.iloc[-1]
    # A bar timestamp is a candle time, not a last-trade timestamp.
    delay = max(0.0, float((current - latest_start - duration).total_seconds()))
    return completed, developing, delay, bool(latest_start <= current)


def render_qh_direct_panel(root, instrument, interval, count, config, *, offset_minutes=65,
                           polling_seconds=5, stale_seconds=180, auto_refresh=True,
                           signal_timing="Candle close only"):
    import numpy as np
    import streamlit as st
    from strategy_engine import run_zscore_backtest, seed_live_intrabar_state, step_live_intrabar_state, live_intrabar_active_frame

    def panel():
        st.header("Direct QH structure OHLC")
        st.caption(f"Selected structure: {instrument}. DFly prices can be calculated from two directly quoted flies when QH does not return the DFly itself.")
        st.button("Refresh direct QH OHLC", key="qh_direct_refresh")
        try:
            client = QHClient(auth_file=Path(root) / ".secrets/qh_authorization.txt", cache_dir=Path(root) / ".cache")
            bars, limits, native = fetch_direct_structure(client, instrument, interval, count, offset_minutes)
            completed, developing, delay, clock_valid = split_direct_candles(bars, interval)
        except (QHAPIError, ValueError) as exc:
            st.error(str(exc))
            st.info("No new live signals are generated on a failed refresh.")
            return
        latest = bars.iloc[-1]
        if bars.attrs.get("derived_dfly"):
            st.info(bars.attrs["source_note"])
        st.caption(f"Checked {pd.Timestamp.now(tz='Europe/London'):%d %b %Y %H:%M:%S %Z}. QH rate limit: {limits.get('remaining') or '—'} remaining of {limits.get('limit') or '—'}. Polling interval: {polling_seconds}s.")
        columns = st.columns(5)
        for column, field in zip(columns, ["open", "high", "low", "close", "volume"]):
            label = field.title()
            if bars.attrs.get("derived_dfly") and field in {"high", "low"}:
                label += " (endpoints)"
            value = "Unavailable" if bars.attrs.get("derived_dfly") and field == "volume" else (f"{latest[field]:.6f}" if field != "volume" else f"{latest[field]:,.0f}")
            column.metric(label, value)
        fresh = clock_valid and delay <= stale_seconds
        st.caption(f"Latest corrected candle time: {latest['strategy_time_london']:%d %b %Y %H:%M:%S %Z}. Raw QH time: {latest['raw_time_utc']:%d %b %Y %H:%M:%S UTC}. Source correction: −{offset_minutes} minutes.")
        if native != interval.upper():
            st.info(f"{interval} candles aggregate QH {native} candles of this exact structure. No outright prices are used.")
        if not fresh:
            st.warning("The latest candle is old or its timestamp is in the future. New intrabar signals are blocked; check source timestamps and the correction setting.")
        st.dataframe(bars.tail(20), hide_index=True, use_container_width=True)
        st.caption("This polls OHLC, not a streaming quote feed. A developing candle is available only if QH returns one. Candle timestamps do not establish the age of the last trade.")
        if config is None or len(completed) <= config.lookback:
            st.info("More completed candles are needed for the Z-score lookback.")
            return
        if bars.attrs.get("derived_dfly") and any(s.stop_loss_ticks is not None for s in config.tranches):
            st.error("Set stop loss to 0 for a calculated DFly: separate fly candles do not provide actual DFly intrabar highs/lows for stop testing.")
            return
        result = run_zscore_backtest(completed, config)
        st.markdown("**Completed-candle model**")
        st.dataframe(result["active"], hide_index=True, use_container_width=True)
        st.dataframe(result["events"].tail(10), hide_index=True, use_container_width=True)
        if signal_timing != "Intrabar live":
            return
        if developing.empty:
            st.info("QH currently supplies completed candles only. No developing-candle intrabar signals are generated.")
            return
        candle = developing.iloc[-1]
        signature = (instrument, interval, config, offset_minutes)
        tracker = st.session_state.get("_qh_direct_tracker")
        if not isinstance(tracker, dict) or tracker.get("signature") != signature:
            tracker = seed_live_intrabar_state(config, result["active"], previous_z=result["summary"]["latest_z"])
            tracker.update(signature=signature, last_sample=None, benchmark_bucket=None)
        bucket = str(candle["strategy_time_london"])
        if tracker.get("benchmark_bucket") != bucket:
            benchmark = completed["close"].iloc[-config.lookback:]
            tracker.update(benchmark_bucket=bucket, mean=float(benchmark.mean()), std=float(benchmark.std(ddof=0)))
        mean, std = tracker["mean"], tracker["std"]
        price = float(candle["close"])
        z = (price - mean) / std if std > 0 else float("nan")
        st.metric(f"Developing {interval} structure Z", f"{z:+.3f}" if np.isfinite(z) else "Unavailable")
        sample = (bucket, price)
        if fresh and np.isfinite(z) and sample != tracker.get("last_sample"):
            tracker, events = step_live_intrabar_state(tracker, config, current_z=z,
                current_price=price, timestamp=pd.Timestamp.now(tz="UTC"))
            tracker["last_sample"] = sample
            if events:
                st.success("New direct structure signal: " + ", ".join(f"{e['action']} T{e['tranche']} ×{e['qty']}" for e in events))
        st.session_state["_qh_direct_tracker"] = tracker
        st.dataframe(live_intrabar_active_frame(tracker), hide_index=True, use_container_width=True)
        st.caption("Intrabar signals follow observed QH closes and can miss movements between polls. Live stop-loss exits are not implemented in the existing intrabar tracker. No orders are submitted.")
    if auto_refresh:
        st.fragment(run_every=polling_seconds)(panel)()
    else:
        panel()
