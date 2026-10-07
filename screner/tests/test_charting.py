import json
from pathlib import Path

from chart_data import epoch_seconds, price_precision, with_whitespace
from data_utils import prepare_ohlc_data


PROJECT_DIR = Path(__file__).resolve().parents[1]


def _sample_frame():
    payload = json.loads(
        (PROJECT_DIR / "sample_data" / "response_1788346938262.json").read_text(
            encoding="utf-8"
        )
    )
    return prepare_ohlc_data(payload, timestamp_offset_minutes=65)


def test_tradingview_component_is_used_and_plotly_is_not_used_by_app():
    app_source = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")
    component_source = (PROJECT_DIR / "chart_component.py").read_text(encoding="utf-8")

    assert "build_ohlc_figure" not in app_source
    assert "LWC.createChart" in component_source
    assert "lightweight-charts@5.2.1" in component_source


def test_tradingview_controls_include_price_scale_drag_and_horizontal_wheel_zoom():
    source = (PROJECT_DIR / "chart_component.py").read_text(encoding="utf-8")

    assert 'mouseWheel: wheelZoom' in source
    assert 'price: interactive' in source
    assert 'axisDoubleClickReset: true' in source
    assert 'pressedMouseMove: interactive' in source


def test_fullscreen_control_is_present_in_tradingview_component():
    source = (PROJECT_DIR / "chart_component.py").read_text(encoding="utf-8")

    assert "qh-tv-fullscreen" in source
    assert "requestFullscreen" in source
    assert "fullscreenchange" in source
    assert "ResizeObserver" in source


def test_light_and_dark_chart_themes_are_supported():
    source = (PROJECT_DIR / "chart_component.py").read_text(encoding="utf-8")
    app_source = (PROJECT_DIR / "app.py").read_text(encoding="utf-8")

    assert 'data.theme !== "light"' in source
    assert 'options=["Dark", "Light"]' in app_source


def test_epoch_times_are_unique_for_sample_bars_and_precision_is_reasonable():
    frame = _sample_frame()
    times = epoch_seconds(frame["strategy_time_london"])
    precision, min_move = price_precision(frame)

    assert len(times) == len(set(times))
    assert 2 <= precision <= 8
    assert min_move == 10 ** (-precision)


def test_uncompressed_gap_view_uses_whitespace_not_fake_candles():
    rows = [
        {"time": 0, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0},
        {"time": 600, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1},
    ]
    expanded = with_whitespace(rows, times=[0, 600], interval="5m")

    assert [row["time"] for row in expanded] == [0, 300, 600]
    assert expanded[1] == {"time": 300}


def test_render_ohlc_chart_does_not_shadow_price_precision():
    source = (PROJECT_DIR / "chart_component.py").read_text(encoding="utf-8")

    assert "precision, min_move = price_precision(frame)" in source
    assert "price_precision, min_move = price_precision(frame)" not in source
    assert '"pricePrecision": int(precision)' in source
