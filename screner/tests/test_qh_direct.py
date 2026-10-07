import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import math
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qh_direct import fetch_direct_structure, split_direct_candles, _DFLY_MISSING
from qh_client import QHResult, QHAPIError


def record(timestamp, price=0.47, product="COF27-G27-H27"):
    return dict(product=product, time=int(pd.Timestamp(timestamp).timestamp()*1000),
                open=price, high=price+0.01, low=price-0.01, close=price, volume=42)


class DirectQHTests(unittest.TestCase):
    def setUp(self):
        _DFLY_MISSING.clear()

    def test_refresh_updates_chart_but_backtest_excludes_developing_bar(self):
        import streamlit as st
        from streamlit.testing.v1 import AppTest
        from strategy_engine import run_zscore_backtest
        now = pd.Timestamp.now(tz="UTC").floor("5min")
        rows = [record(now - pd.Timedelta(minutes=5 * (100-i)) + pd.Timedelta(minutes=65), .5+.05*math.sin(i)) for i in range(101)]
        script = (ROOT / "app.py").read_text(encoding="utf-8")
        script = script.replace('default="Sample",', 'default="QH API",')
        script = script.replace('qh_token_ready = render_qh_access_panel()', 'qh_token_ready = True')
        with patch("qh_client.QHClient.fetch_ohlc", return_value=QHResult(rows, {}, False)), \
             patch("chart_component.render_ohlc_chart") as chart, \
             patch("strategy_engine.run_zscore_backtest", wraps=run_zscore_backtest) as backtest, \
             patch("streamlit.fragment", wraps=st.fragment) as fragments:
            app = AppTest.from_string(script).run(timeout=30)
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(any(c.kwargs.get("run_every") == 5 for c in fragments.call_args_list))
            self.assertEqual(len(chart.call_args.args[0]), 101)
            self.assertTrue(all(len(call.args[0]) == 100 for call in backtest.call_args_list))
            rows[-1].update(open=.8, high=.81, low=.79, close=.8)
            app.run(timeout=30)
            self.assertEqual(len(app.exception), 0)
            self.assertAlmostEqual(chart.call_args.args[0].iloc[-1]["close"], .8)
            self.assertTrue(all(len(call.args[0]) == 100 for call in backtest.call_args_list))
            self.assertTrue(any(t.label == "Auto refresh QH charts and results" and t.value for t in app.toggle))

    def test_dfly_uses_two_quoted_flies_at_matching_times(self):
        client = Mock()
        first = "COF27-G27-H27"
        second = "COG27-H27-J27"
        fly_rows = [record("2026-10-07T10:00:00Z", .47, first),
                    record("2026-10-07T10:05:00Z", .55, first),
                    record("2026-10-07T10:00:00Z", .20, second)]
        client.fetch_ohlc.side_effect = [QHResult([], {}, False), QHResult(fly_rows, {}, False)]
        frame, _, _ = fetch_direct_structure(client, "COF27-G27-H27-J27", "5M", 500, 0)
        self.assertEqual(client.fetch_ohlc.call_args_list[1].kwargs["instruments"], first + "," + second)
        self.assertEqual(len(frame), 1)
        self.assertAlmostEqual(frame.iloc[0]["close"], .27)
        self.assertTrue(frame.attrs["derived_dfly"])
        self.assertEqual(frame.attrs["components"], (first, second))
        self.assertEqual(frame.iloc[0]["high"], frame.iloc[0]["close"])

    def test_direct_dfly_is_preferred_when_available(self):
        client = Mock()
        client.fetch_ohlc.return_value = QHResult([record("2026-10-07T10:00:00Z", .27, "COF27-G27-H27-J27")], {}, False)
        frame, _, _ = fetch_direct_structure(client, "COF27-G27-H27-J27", "5M", 500, 0)
        self.assertEqual(client.fetch_ohlc.call_count, 1)
        self.assertFalse(frame.attrs.get("derived_dfly", False))

    def test_dfly_requires_matching_component_data(self):
        client = Mock()
        client.fetch_ohlc.side_effect = [QHResult([], {}, False), QHResult([
            record("2026-10-07T10:00:00Z", .47, "COF27-G27-H27"),
            record("2026-10-07T10:05:00Z", .20, "COG27-H27-J27")], {}, False)]
        with self.assertRaises(QHAPIError):
            fetch_direct_structure(client, "COF27-G27-H27-J27", "5M", 500, 0)

    def test_full_app_dfly_mode(self):
        from streamlit.testing.v1 import AppTest
        now = pd.Timestamp.now(tz="UTC").floor("5min")
        first, second = "COF27-G27-H27", "COG27-H27-J27"
        fly_rows = [record(now-pd.Timedelta(minutes=5*(100-i))+pd.Timedelta(minutes=65),
                   .5+.05*math.sin(i), first) for i in range(101)]
        fly_rows += [record(now-pd.Timedelta(minutes=5*(100-i))+pd.Timedelta(minutes=65), .2, second) for i in range(101)]
        def response(**kwargs):
            code = kwargs["instruments"]
            self.assertIn(code, {"COF27-G27-H27-J27", first + "," + second})
            return QHResult(fly_rows if "," in code else [], {}, False)
        script = (ROOT / "app.py").read_text(encoding="utf-8")
        script = script.replace('default="Sample",', 'default="QH API + Live",')
        script = script.replace('value="COF27-G27-H27",', 'value="COF27-G27-H27-J27",')
        script = script.replace('qh_token_ready = render_qh_access_panel()', 'qh_token_ready = True')
        script = script.replace('st.toggle("Poll direct QH OHLC automatically", value=True)', 'st.toggle("Poll direct QH OHLC automatically", value=False)')
        with patch("qh_client.QHClient.fetch_ohlc", side_effect=response), patch("chart_component.render_ohlc_chart"):
            app = AppTest.from_string(script).run(timeout=30)
            self.assertEqual(len(app.exception), 0, [str(x.message) for x in app.exception])
            self.assertEqual(len(app.error), 0, [str(x.value) for x in app.error])
            self.assertTrue(any("Calculated DFly:" in str(x.value) for x in app.info))

    def test_requests_one_exact_structure_and_preserves_ohlc(self):
        client = Mock()
        client.fetch_ohlc.return_value = QHResult([record("2026-10-07T10:00:00Z")], {}, False)
        frame, _, native = fetch_direct_structure(client, "COF27-G27-H27", "5M", 500, 0)
        client.fetch_ohlc.assert_called_once_with(instruments="COF27-G27-H27", interval="5M", count=500, cache_ttl_seconds=5)
        self.assertEqual(frame.iloc[0][["open", "high", "low", "close", "volume"]].tolist(), [0.47, 0.48, 0.45999999999999996, 0.47, 42])
        self.assertEqual(native, "5M")

    def test_wrong_instrument_is_rejected_without_fallback(self):
        client = Mock()
        client.fetch_ohlc.return_value = QHResult([record("2026-10-07T10:00:00Z", product="COF27")], {}, False)
        with self.assertRaises(QHAPIError):
            fetch_direct_structure(client, "COF27-G27-H27", "5M", 500, 0)
        self.assertEqual(client.fetch_ohlc.call_count, 1)

    def test_larger_interval_uses_same_structure(self):
        client = Mock()
        client.fetch_ohlc.return_value = QHResult([record(f"2026-10-07T10:{minute:02d}:00Z", price)
            for minute, price in [(0, .4), (5, .6), (10, .5)]], {}, False)
        frame, _, native = fetch_direct_structure(client, "COF27-G27-H27", "15M", 500, 0)
        self.assertEqual(client.fetch_ohlc.call_args.kwargs["instruments"], "COF27-G27-H27")
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]["high"], .61)
        self.assertEqual(frame.iloc[0]["close"], .5)

    def test_completed_developing_and_stale_candles(self):
        client = Mock()
        client.fetch_ohlc.return_value = QHResult([record("2026-10-07T09:55:00Z"), record("2026-10-07T10:00:00Z")], {}, False)
        frame, _, _ = fetch_direct_structure(client, "COF27-G27-H27", "5M", 500, 0)
        completed, developing, delay, valid = split_direct_candles(frame, "5M", "2026-10-07T10:02:00Z")
        self.assertEqual((len(completed), len(developing), delay, valid), (1, 1, 0, True))
        self.assertEqual(split_direct_candles(frame, "5M", "2026-10-07T10:20:00Z")[2], 900)
        self.assertFalse(split_direct_candles(frame, "5M", "2026-10-07T09:50:00Z")[3])

    def test_full_app_direct_mode(self):
        from streamlit.testing.v1 import AppTest
        now = pd.Timestamp.now(tz="UTC").floor("5min")
        rows = [record(now - pd.Timedelta(minutes=5 * (100-i)) + pd.Timedelta(minutes=65), .5+.05*math.sin(i)) for i in range(101)]
        script = (ROOT / "app.py").read_text(encoding="utf-8")
        script = script.replace('default="Sample",', 'default="QH API + Live",')
        script = script.replace('qh_token_ready = render_qh_access_panel()', 'qh_token_ready = True')
        script = script.replace('st.toggle("Poll direct QH OHLC automatically", value=True)', 'st.toggle("Poll direct QH OHLC automatically", value=False)')
        with patch("qh_client.QHClient.fetch_ohlc", return_value=QHResult(rows, {"remaining":"29", "limit":"30"}, False)) as fetch, patch("chart_component.render_ohlc_chart"):
            app = AppTest.from_string(script).run(timeout=30)
            self.assertEqual(len(app.exception), 0, [str(x.message) for x in app.exception])
            self.assertGreaterEqual(fetch.call_count, 2)
            for call in fetch.call_args_list:
                self.assertEqual(call.kwargs["instruments"], "COF27-G27-H27")


if __name__ == "__main__":
    unittest.main()
