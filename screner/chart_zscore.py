"""Z-score of the latest selected-timeframe candle against prior candles."""
import math
import pandas as pd
from data_utils import interval_to_timedelta


def chart_z_snapshot(frame, lookback, interval, *, now=None, benchmark=None):
    if lookback < 2:
        raise ValueError("Z lookback must be at least 2 candles.")
    duration = interval_to_timedelta(interval)
    if duration is None or frame.empty:
        raise ValueError("A supported interval and at least one candle are required.")
    current = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now).tz_convert("UTC")
    latest = frame.iloc[-1]
    start = pd.Timestamp(latest["strategy_time_london"]).tz_convert("UTC")
    end = start + duration
    developing = start <= current < end
    history = frame.iloc[:-1]
    # Ignore the latest candle itself and any overlapping/future source candles.
    prior_ends = history["strategy_time_london"].dt.tz_convert("UTC") + duration
    prices = pd.to_numeric(history.loc[prior_ends <= start, "close"], errors="coerce").dropna().tail(lookback)
    count = len(prices)
    mean = float(prices.mean()) if count == lookback else float("nan")
    std = float(prices.std(ddof=0)) if count == lookback else float("nan")
    if benchmark is not None:
        mean, std, count = benchmark["mean"], benchmark["std"], benchmark["count"]
    price = float(latest["close"])
    z = (price - mean) / std if count == lookback and math.isfinite(std) and std > 0 else float("nan")
    return dict(price=price, mean=mean, std=std, z=z, count=count,
                start=start, end=end, developing=developing, future=start > current,
                delay=max(0.0, (current-end).total_seconds()))


def render_chart_zscore(frame, interval, *, source_key, stale_seconds=180, live_source=False):
    import streamlit as st
    columns = st.columns([1.3, 1, 1, 1, 1])
    with columns[0]:
        lookback = int(st.number_input("Z-score lookback (candles)", min_value=2,
            max_value=5000, value=40, step=1, key="z_lookback",
            help="Uses this many previous completed candles at the selected interval. Also controls Strategy Lab and live signal calculations."))
    info = chart_z_snapshot(frame, lookback, interval)
    key = (source_key, interval, lookback, str(info["start"]))
    frozen = st.session_state.get("_chart_z_benchmark")
    if info["developing"]:
        if isinstance(frozen, dict) and frozen.get("key") == key and frozen.get("count") == lookback:
            info = chart_z_snapshot(frame, lookback, interval, benchmark=frozen)
        elif info["count"] == lookback:
            st.session_state["_chart_z_benchmark"] = dict(key=key, mean=info["mean"], std=info["std"], count=info["count"])
    label = f"LIVE Z · {interval}" if info["developing"] else f"Latest candle Z · {interval}"
    columns[1].metric(label, f"{info['z']:+.3f}" if math.isfinite(info["z"]) else "Unavailable")
    columns[2].metric("Latest close", f"{info['price']:.6f}")
    columns[3].metric("Benchmark mean", f"{info['mean']:.6f}" if math.isfinite(info["mean"]) else "Unavailable")
    columns[4].metric("Benchmark SD", f"{info['std']:.6f}" if math.isfinite(info["std"]) else "Unavailable")
    phase = "developing" if info["developing"] else "completed"
    st.caption(f"Z = (latest close − mean) / SD of the previous {lookback} completed {interval} candles. The latest candle is excluded from its benchmark. {phase.title()} candle: {info['start'].tz_convert('Europe/London'):%d %b %Y %H:%M:%S %Z}. The benchmark stays fixed while this candle develops.")
    if info["count"] < lookback:
        st.info(f"Only {info['count']} prior completed candles are available; this setting needs {lookback}. Increase Maximum bars or lower the lookback.")
    elif not math.isfinite(info["z"]):
        st.info("Z-score is unavailable because the benchmark has no usable price variation.")
    if live_source and (info["future"] or info["delay"] > stale_seconds):
        st.warning("This Z-score uses an old or future-dated candle. Check the candle timestamp and source correction before treating it as live.")
    elif live_source and not info["developing"]:
        st.caption("QH has not supplied a developing candle. The value above is the latest completed candle's Z-score and refreshes as new data arrives.")
    return info
