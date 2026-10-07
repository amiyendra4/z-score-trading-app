import sys
from pathlib import Path
import unittest
from unittest.mock import patch
import math
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chart_zscore import chart_z_snapshot
from qh_client import QHResult


def price_frame(values, step="5min"):
    return pd.DataFrame({"strategy_time_london": pd.date_range("2026-10-07T10:00:00Z", periods=len(values), freq=step).tz_convert("Europe/London"), "close": values})


class ChartZTests(unittest.TestCase):
    def test_uses_previous_candles_excluding_current(self):
        info = chart_z_snapshot(price_frame([1, 3, 5]), 2, "5M", now="2026-10-07T10:12:00Z")
        self.assertEqual((info["mean"], info["std"], info["z"]), (2, 1, 3))
        self.assertTrue(info["developing"])

    def test_benchmark_is_fixed_during_developing_candle(self):
        first = chart_z_snapshot(price_frame([1, 3, 5]), 2, "5M", now="2026-10-07T10:12:00Z")
        second = chart_z_snapshot(price_frame([2, 6, 7]), 2, "5M", now="2026-10-07T10:13:00Z", benchmark=first)
        self.assertEqual((second["mean"], second["std"], second["z"]), (2, 1, 5))

    def test_selected_interval_controls_candle_status(self):
        five = chart_z_snapshot(price_frame([1, 3, 5]), 2, "5M", now="2026-10-07T10:20:00Z")
        hourly = chart_z_snapshot(price_frame([1, 3, 5], "1h"), 2, "1H", now="2026-10-07T12:20:00Z")
        self.assertFalse(five["developing"])
        self.assertTrue(hourly["developing"])

    def test_insufficient_history_and_zero_sd(self):
        self.assertTrue(math.isnan(chart_z_snapshot(price_frame([1, 2]), 2, "5M")["z"]))
        self.assertTrue(math.isnan(chart_z_snapshot(price_frame([1, 1, 2]), 2, "5M")["z"]))

    def test_app_chart_input_and_interval_switch(self):
        from streamlit.testing.v1 import AppTest
        requests = []
        def fetch(**kwargs):
            interval = kwargs["interval"]
            requests.append(interval)
            step = pd.Timedelta(minutes=5) if interval == "5M" else pd.Timedelta(hours=1)
            now = pd.Timestamp.now(tz="UTC").floor("5min" if interval == "5M" else "1h")
            rows = []
            for i in range(101):
                price = .5 + .05*math.sin(i if interval == "5M" else i/3)
                timestamp = now - step * (100-i) + pd.Timedelta(minutes=65)
                rows.append(dict(product="COF27-G27-H27", time=int(timestamp.timestamp()*1000),
                    open=price, high=price+.01, low=price-.01, close=price, volume=1))
            return QHResult(rows, {}, False)
        script = (ROOT / "app.py").read_text(encoding="utf-8")
        script = script.replace('default="Sample",', 'default="QH API",')
        script = script.replace('qh_token_ready = render_qh_access_panel()', 'qh_token_ready = True')
        with patch("qh_client.QHClient.fetch_ohlc", side_effect=fetch), patch("chart_component.render_ohlc_chart"):
            app = AppTest.from_string(script).run(timeout=30)
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(any(m.label == "LIVE Z · 5M" and m.value != "Unavailable" for m in app.metric))
            self.assertEqual(app.number_input(key="z_lookback").value, 40)
            app.number_input(key="z_lookback").set_value(20).run(timeout=30)
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(any(m.label == "Z lookback (candles)" and m.value == "20" for m in app.metric))
            interval_control = next(x for x in app.selectbox if x.label == "Candle interval")
            interval_control.set_value("1H").run(timeout=30)
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(requests[-1], "1H")
            self.assertEqual(app.number_input(key="z_lookback").value, 20)
            self.assertTrue(any(m.label == "LIVE Z · 1H" and m.value != "Unavailable" for m in app.metric))


if __name__ == "__main__":
    unittest.main()
