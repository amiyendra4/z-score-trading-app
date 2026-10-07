import json
import unittest
from unittest.mock import patch
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from insight_prices import parse_insight_text, build_insight_snapshot, InsightPriceError, iter_insight_rows, select_insight_rows, fetch_insight, _HTTP_CACHE
from strategy_engine import StrategyConfig, TrancheSpec


class InsightTests(unittest.TestCase):
    def test_actual_nested_history_shape(self):
        names = [f"ENERGY_{i}" for i in range(370)]
        row = ["2026-05-08T14:31:00.000000000Z"] + list(range(370))
        payload = [names, [row] * 100]
        body = json.dumps(payload).encode()
        for chunk_size in (7, 65536):
            chunks = (body[i:i+chunk_size] for i in range(0, len(body), chunk_size))
            symbols, rows = select_insight_rows(iter_insight_rows(chunks), "ENERGY_369")
            self.assertEqual(symbols, ["ENERGY_369"])
            self.assertEqual(len(rows), 100)
            self.assertEqual(rows[-1], [row[0], 369])
        self.assertEqual(parse_insight_text(json.dumps(payload)), (names, payload[1]))

    def test_normal_full_size_http_chunks(self):
        names = [f"STRUCTURE_{i}" for i in range(700)]
        row = ["2026-10-06T10:00:00.000000000Z"] + list(range(700))
        body = json.dumps([names] + [row] * 30).encode()
        self.assertGreater(len(body), 65536)
        def chunks():
            return (body[i:i+65536] for i in range(0, len(body), 65536))
        self.assertEqual(select_insight_rows(iter_insight_rows(chunks()))[0], names)
        selected_names, rows = select_insight_rows(iter_insight_rows(chunks()), "STRUCTURE_699")
        self.assertEqual(selected_names, ["STRUCTURE_699"])
        self.assertEqual(len(rows), 30)
        self.assertEqual(rows[-1][1], 699)

    def test_conditional_download_reuses_unchanged_file(self):
        import unittest.mock as mock
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.headers = {"ETag": '"version1"', "Last-Modified": "Tue, 06 Oct 2026 10:00:00 GMT"}
        response.iter_content.return_value = iter([b'[["A"],["2026-10-06T10:00:00Z",1]]'])
        fake_requests = types.ModuleType("requests")
        fake_requests.get = mock.Mock(return_value=response)
        fake_requests.RequestException = RuntimeError
        fake_requests.exceptions = types.SimpleNamespace(SSLError=OSError)
        _HTTP_CACHE.clear()
        with patch.dict(sys.modules, {"requests": fake_requests}):
            first = fetch_insight("https://example.test/array", "A")
            _HTTP_CACHE[("https://example.test/array", "A")]["checked"] -= 10
            response.status_code = 304
            response.iter_content.reset_mock()
            self.assertEqual(fetch_insight("https://example.test/array", "A"), first)
            self.assertEqual(fake_requests.get.call_args.kwargs["headers"]["If-None-Match"], '"version1"')
            response.iter_content.assert_not_called()
        _HTTP_CACHE.clear()

    def test_streaming_chunk_boundaries_and_assignments(self):
        payload = [["A", "B"], ["2026-10-06T10:00:00Z", 2, -0.5], ["2026-10-06T10:01:00Z", 3, 0.1]]
        body = ("\ufeffvar prices = " + json.dumps(payload) + ";").encode("utf-8")
        for size in (1, 7, 64):
            rows = iter_insight_rows(body[i:i+size] for i in range(0, len(body), size))
            names, selected = select_insight_rows(rows, "B")
            self.assertEqual(names, ["B"])
            self.assertEqual(selected, [[payload[1][0], -0.5], [payload[2][0], 0.1]])

    def test_header_does_not_download_history(self):
        def chunks():
            yield b'[["A","B"],'
            raise AssertionError("Header discovery must close before reading history")
        self.assertEqual(select_insight_rows(iter_insight_rows(chunks())), (["A", "B"], []))

    def test_streamed_file_above_previous_limit(self):
        def chunks():
            yield b'[["A"],'
            # Over 64 MB of legal JSON whitespace without building a giant string.
            for _ in range(1100):
                yield b' ' * 65536
            yield b'["2026-10-06T10:00:00Z",1]]'
        names, rows = select_insight_rows(iter_insight_rows(chunks()), "A")
        self.assertEqual(rows, [["2026-10-06T10:00:00Z", 1]])

    def test_stream_keeps_latest_rows_in_unsorted_input(self):
        rows = iter([["A", "B"], ["2026-10-06T10:05:00Z", 5, 50],
                     ["2026-10-06T10:01:00Z", 1, 10], ["2026-10-06T10:03:00Z", 3, 30]])
        self.assertEqual(select_insight_rows(rows, "A", max_rows=2)[1],
            [["2026-10-06T10:03:00Z", 3], ["2026-10-06T10:05:00Z", 5]])

    def test_stream_rejects_truncated_rows_and_trailing_code(self):
        for body in [b'[["A"],["2026-10-06",1', b'[["A"],["2026-10-06",1]];alert(1)', b'[["A"],{"x":1}]']:
            with self.assertRaises(InsightPriceError):
                list(iter_insight_rows([body]))

    def test_plain_and_assigned_json(self):
        payload = [["LCOX6-Z6", "CLX26-Z26"], ["2026-10-06T10:00:00.000000000Z", -0.25, 1.0]]
        for wrapper in [lambda s: s, lambda s: "var prices = " + s + ";", lambda s: "\ufeffconst prices=" + s + ";"]:
            names, rows = parse_insight_text(wrapper(json.dumps(payload)))
            self.assertEqual(names, payload[0])
            self.assertEqual(rows, payload[1:])

    def test_rejects_html_code_and_bad_columns(self):
        for text in ["<html>sign in</html>", 'alert("bad")', '[["A"],["2026-10-06",1,2]]', '[["A","A"],["2026-10-06",1,2]]']:
            with self.assertRaises(InsightPriceError):
                parse_insight_text(text)

    def test_candle_aggregation_excludes_developing(self):
        rows = [["2026-10-06T10:00:00Z", 1], ["2026-10-06T10:01:00Z", 3],
                ["2026-10-06T10:03:00Z", 2], ["2026-10-06T10:05:00Z", 4],
                ["2026-10-06T10:06:00Z", 5]]
        snap = build_insight_snapshot(["A"], rows, "A", "5M", now="2026-10-06T10:06:30Z")
        self.assertEqual(len(snap.completed), 1)
        self.assertEqual(len(snap.developing), 1)
        self.assertEqual(snap.completed.iloc[0][["open", "high", "low", "close"]].tolist(), [1, 3, 1, 2])
        self.assertEqual(snap.latest_price, 5)
        self.assertEqual(snap.age_seconds, 30)

    def test_missing_gaps_negative_prices_and_duplicate_timestamps(self):
        rows = [["2026-10-06T10:00:00Z", -0.25], ["2026-10-06T10:01:00Z", -100],
                ["2026-10-06T10:02:00Z", None], ["2026-10-06T10:10:00Z", -0.1],
                ["2026-10-06T10:10:00Z", -0.2]]
        snap = build_insight_snapshot(["A"], rows, "A", "5M", now="2026-10-06T10:20:00Z")
        self.assertEqual(snap.skipped, 2)
        self.assertEqual(len(snap.completed), 2)
        self.assertEqual(snap.latest_price, -0.2)
        self.assertEqual(snap.age_seconds, 600)

    def test_sentinel_can_be_disabled(self):
        snap = build_insight_snapshot(["A"], [["2026-10-06T10:00:00Z", -100]], "A", "1M", missing_value=None, now="2026-10-06T10:02:00Z")
        self.assertEqual(snap.latest_price, -100)

    def test_naive_timezone_and_offset(self):
        snap = build_insight_snapshot(["A"], [["2026-10-06T11:00:00", 2]], "A", "5M",
            naive_timezone="Europe/London", offset_minutes=5, now="2026-10-06T10:10:00Z")
        self.assertEqual(str(snap.latest_time), "2026-10-06 09:55:00+00:00")

    def test_unknown_symbol_and_invalid_timestamp(self):
        with self.assertRaises(InsightPriceError):
            build_insight_snapshot(["LCO"], [["2026-10-06T10:00:00Z", 1]], "CO", "5M")
        with self.assertRaises(InsightPriceError):
            build_insight_snapshot(["A"], [["not a date", 1]], "A", "5M")

    def test_live_panel_fresh_stale_and_candle_close(self):
        # Exercise the real panel and strategy logic without requiring a browser or corporate connection.
        class FakeStreamlit(types.ModuleType):
            def __init__(self):
                super().__init__("streamlit")
                self.session_state = {}
                self.calls = []
            def fragment(self, **kwargs):
                return lambda fn: fn
            def columns(self, count):
                return [self] * count
            def button(self, *args, **kwargs):
                return False
            def __getattr__(self, name):
                return lambda *args, **kwargs: self.calls.append((name, args))
        fake = FakeStreamlit()
        with patch.dict(sys.modules, {"streamlit": fake}):
            sys.modules.pop("insight_panel", None)
            import insight_panel
        config = StrategyConfig(lookback=5, tranches=(TrancheSpec(1.5, 1.0, 1),))
        rows = [[f"2026-10-06T10:{minute:02d}:00Z", 1 + minute % 3] for minute in range(11)]
        fresh = build_insight_snapshot(["A"], rows, "A", "1M", now="2026-10-06T10:10:30Z")
        stale = build_insight_snapshot(["A"], rows, "A", "1M", now="2026-10-06T10:20:00Z")
        with patch.object(insight_panel, "fetch_insight", return_value=(["A"], rows)):
            with patch.object(insight_panel, "build_insight_snapshot", return_value=fresh):
                insight_panel.render_insight_panel("https://example.test/prices", "A", "1M", config)
                tracker = fake.session_state["_insight_intrabar_tracker"]
                self.assertEqual(tracker["last_timestamp"], fresh.latest_time.value)
                # Repeated timestamp must not produce another signal observation.
                with patch.object(insight_panel, "step_live_intrabar_state") as step:
                    insight_panel.render_insight_panel("https://example.test/prices", "A", "1M", config)
                    step.assert_not_called()
                insight_panel.render_insight_panel("https://example.test/prices", "A", "1M", config, signal_timing="Candle close only")
            with patch.object(insight_panel, "build_insight_snapshot", return_value=stale), patch.object(insight_panel, "step_live_intrabar_state") as step:
                insight_panel.render_insight_panel("https://example.test/prices", "A", "1M", config)
                step.assert_not_called()
                self.assertTrue(any(name == "warning" for name, args in fake.calls))


if __name__ == "__main__":
    unittest.main()
