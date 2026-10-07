from __future__ import annotations

import pandas as pd
import streamlit as st

from chart_data import epoch_seconds, price_precision, with_whitespace
from data_utils import interval_to_timedelta


# TradingView Lightweight Charts is loaded in the browser. This keeps the Python
# app Node-free while using the native financial-chart interaction model.
# Version is pinned so an upstream release cannot silently change behavior.
_LWC_PRIMARY = "https://unpkg.com/lightweight-charts@5.2.1/dist/lightweight-charts.standalone.production.js"
_LWC_FALLBACK = "https://cdn.jsdelivr.net/npm/lightweight-charts@5.2.1/dist/lightweight-charts.standalone.production.js"

_COMPONENT_HTML = """
<div class="qh-tv-shell">
  <div class="qh-tv-chart"></div>
  <div class="qh-tv-readout" aria-live="polite"></div>
  <div class="qh-tv-actions">
    <button class="qh-tv-reset" type="button" title="Fit all bars and reset the price scale">Reset</button>
    <button class="qh-tv-fullscreen" type="button" title="Open chart full screen">Full screen</button>
  </div>
  <div class="qh-tv-error" role="alert" hidden></div>
</div>
"""

_COMPONENT_CSS = """
.qh-tv-shell {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 420px;
  overflow: hidden;
  background: #0e1117;
}

.qh-tv-shell:fullscreen,
.qh-tv-shell.qh-tv-fullscreen-fallback {
  width: 100vw !important;
  height: 100vh !important;
  min-width: 100vw !important;
  min-height: 100vh !important;
  background: #0e1117;
}

.qh-tv-shell.qh-tv-fullscreen-fallback {
  position: fixed !important;
  inset: 0 !important;
  z-index: 2147483647 !important;
}

.qh-tv-shell::backdrop {
  background: #0e1117;
}

.qh-tv-chart {
  position: absolute;
  inset: 0;
  width: 100% !important;
  height: 100% !important;
}

.qh-tv-readout {
  position: absolute;
  z-index: 20;
  top: 8px;
  left: 10px;
  max-width: calc(100% - 210px);
  padding: 4px 7px;
  border-radius: 4px;
  color: #f0f2f6;
  background: rgba(14, 17, 23, 0.80);
  font: 12px/1.35 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  pointer-events: none;
}

.qh-tv-actions {
  position: absolute;
  z-index: 30;
  top: 7px;
  right: 9px;
  display: flex;
  gap: 6px;
}

.qh-tv-actions button {
  border: 1px solid rgba(240, 242, 246, 0.38);
  border-radius: 5px;
  padding: 5px 9px;
  color: #f0f2f6;
  background: rgba(14, 17, 23, 0.86);
  font: 600 12px/1.2 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  cursor: pointer;
  user-select: none;
}

.qh-tv-actions button:hover {
  background: rgba(49, 51, 63, 0.95);
  border-color: rgba(240, 242, 246, 0.72);
}

.qh-tv-error {
  position: absolute;
  inset: 50px 20px auto 20px;
  z-index: 50;
  padding: 12px 14px;
  border: 1px solid #e05260;
  border-radius: 6px;
  color: #f0f2f6;
  background: rgba(116, 28, 36, 0.95);
  font: 13px/1.45 ui-sans-serif, system-ui, sans-serif;
}
"""

_COMPONENT_JS = r"""
const instances = new WeakMap()

function loadExternalScript(doc, src, id) {
  return new Promise((resolve, reject) => {
    const win = doc.defaultView
    if (win?.LightweightCharts) {
      resolve(win.LightweightCharts)
      return
    }

    const existing = doc.getElementById(id)
    if (existing) {
      existing.addEventListener("load", () => resolve(win.LightweightCharts), { once: true })
      existing.addEventListener("error", reject, { once: true })
      return
    }

    const script = doc.createElement("script")
    script.id = id
    script.src = src
    script.async = true
    script.onload = () => resolve(win.LightweightCharts)
    script.onerror = reject
    doc.head.appendChild(script)
  })
}

async function ensureLightweightCharts(doc, primaryUrl, fallbackUrl) {
  const win = doc.defaultView
  if (win?.LightweightCharts) return win.LightweightCharts

  try {
    const library = await loadExternalScript(doc, primaryUrl, "qh-lwc-primary-v521")
    if (library) return library
  } catch (error) {
    // Try the second CDN below.
  }

  const stale = doc.getElementById("qh-lwc-primary-v521")
  stale?.remove?.()

  const library = await loadExternalScript(doc, fallbackUrl, "qh-lwc-fallback-v521")
  if (!library) throw new Error("Lightweight Charts loaded without creating the global API.")
  return library
}

export default function (component) {
  const { data, parentElement } = component
  const shell = parentElement.querySelector(".qh-tv-shell")
  const container = parentElement.querySelector(".qh-tv-chart")
  const readout = parentElement.querySelector(".qh-tv-readout")
  const fullscreenButton = parentElement.querySelector(".qh-tv-fullscreen")
  const resetButton = parentElement.querySelector(".qh-tv-reset")
  const errorBox = parentElement.querySelector(".qh-tv-error")
  if (!shell || !container || !readout || !fullscreenButton || !resetButton || !errorBox || !data) return

  const previous = instances.get(parentElement)
  previous?.destroy?.()

  let destroyed = false
  let chart = null
  let resizeObserver = null
  let fullscreenCleanup = null

  const ownerDocument = shell.ownerDocument
  const ownerWindow = ownerDocument.defaultView

  const showError = (message) => {
    if (destroyed) return
    errorBox.hidden = false
    errorBox.textContent = message
  }

  const isFullscreen = () =>
    ownerDocument.fullscreenElement === shell ||
    shell.classList.contains("qh-tv-fullscreen-fallback")

  const setFullscreenButtonState = () => {
    const active = isFullscreen()
    fullscreenButton.textContent = active ? "Exit full screen" : "Full screen"
    fullscreenButton.title = active ? "Exit full screen" : "Open chart full screen"
  }

  const exitFallbackFullscreen = () => {
    if (!shell.classList.contains("qh-tv-fullscreen-fallback")) return false
    shell.classList.remove("qh-tv-fullscreen-fallback")
    setFullscreenButtonState()
    return true
  }

  const toggleFullscreen = async (event) => {
    event?.preventDefault?.()
    event?.stopPropagation?.()

    if (ownerDocument.fullscreenElement === shell) {
      await ownerDocument.exitFullscreen?.()
      return
    }
    if (exitFallbackFullscreen()) return

    if (shell.requestFullscreen) {
      try {
        await shell.requestFullscreen()
        return
      } catch (error) {
        // Embedded-browser policies can deny element fullscreen; use a best-effort fallback.
      }
    }

    shell.classList.add("qh-tv-fullscreen-fallback")
    setFullscreenButtonState()
  }

  const handleFullscreenChange = () => setFullscreenButtonState()
  const handleKeyDown = (event) => {
    if (event.key === "Escape") exitFallbackFullscreen()
  }

  fullscreenButton.onclick = toggleFullscreen
  ownerDocument.addEventListener("fullscreenchange", handleFullscreenChange)
  ownerDocument.addEventListener("keydown", handleKeyDown)
  setFullscreenButtonState()

  const destroy = () => {
    if (destroyed) return
    destroyed = true
    resizeObserver?.disconnect?.()
    fullscreenCleanup?.()
    fullscreenButton.onclick = null
    resetButton.onclick = null
    ownerDocument.removeEventListener("fullscreenchange", handleFullscreenChange)
    ownerDocument.removeEventListener("keydown", handleKeyDown)
    shell.classList.remove("qh-tv-fullscreen-fallback")
    chart?.remove?.()
    chart = null
    if (instances.get(parentElement)?.destroy === destroy) instances.delete(parentElement)
  }

  instances.set(parentElement, { destroy })

  ;(async () => {
    try {
      const LWC = await ensureLightweightCharts(ownerDocument, data.primaryCdn, data.fallbackCdn)
      if (destroyed) return

      const darkTheme = data.theme !== "light"
      const background = darkTheme ? "#0e1117" : "#ffffff"
      const textColor = darkTheme ? "#d8dee9" : "#252a34"
      const gridColor = darkTheme ? "rgba(128,128,128,0.16)" : "rgba(70,70,70,0.12)"
      const borderColor = darkTheme ? "rgba(128,128,128,0.28)" : "rgba(70,70,70,0.22)"
      const crosshairColor = darkTheme ? "rgba(216,222,233,0.78)" : "rgba(55,61,71,0.62)"
      const navMode = data.navigationMode ?? "Time zoom"
      const interactive = navMode !== "Page scroll"
      const wheelZoom = navMode === "Time zoom"

      shell.style.backgroundColor = background
      readout.style.color = textColor
      readout.style.background = darkTheme ? "rgba(14,17,23,0.80)" : "rgba(255,255,255,0.88)"
      for (const button of [fullscreenButton, resetButton]) {
        button.style.color = textColor
        button.style.background = darkTheme ? "rgba(14,17,23,0.86)" : "rgba(255,255,255,0.90)"
        button.style.borderColor = borderColor
      }

      const londonTickFormatter = (time) => {
        const seconds = typeof time === "number" ? time : null
        if (seconds === null) return String(time ?? "")
        const date = new Date(seconds * 1000)
        return new Intl.DateTimeFormat("en-GB", {
          timeZone: "Europe/London",
          day: "2-digit",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        }).format(date)
      }

      const londonTimeFormatter = (time) => {
        const seconds = typeof time === "number" ? time : null
        if (seconds === null) return String(time ?? "")
        const date = new Date(seconds * 1000)
        return new Intl.DateTimeFormat("en-GB", {
          timeZone: "Europe/London",
          day: "2-digit",
          month: "short",
          year: "numeric",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
          timeZoneName: "short",
        }).format(date)
      }

      chart = LWC.createChart(container, {
        autoSize: true,
        layout: {
          background: { type: LWC.ColorType.Solid, color: background },
          textColor,
          attributionLogo: true,
          panes: {
            enableResize: true,
            separatorColor: borderColor,
            separatorHoverColor: darkTheme ? "rgba(216,222,233,0.18)" : "rgba(55,61,71,0.14)",
          },
        },
        grid: {
          vertLines: { color: gridColor },
          horzLines: { color: gridColor },
        },
        crosshair: {
          mode: LWC.CrosshairMode.Normal,
          vertLine: { color: crosshairColor, width: 1, style: 3, labelBackgroundColor: darkTheme ? "#3b404a" : "#6b7280" },
          horzLine: { color: crosshairColor, width: 1, style: 3, labelBackgroundColor: darkTheme ? "#3b404a" : "#6b7280" },
        },
        rightPriceScale: {
          visible: true,
          borderColor,
          autoScale: true,
          scaleMargins: { top: 0.08, bottom: 0.08 },
        },
        timeScale: {
          borderColor,
          timeVisible: true,
          secondsVisible: Boolean(data.secondsVisible),
          rightOffset: 2,
          barSpacing: 7,
          minBarSpacing: 0.5,
          fixLeftEdge: false,
          fixRightEdge: false,
          lockVisibleTimeRangeOnResize: true,
          tickMarkFormatter: londonTickFormatter,
        },
        localization: {
          locale: "en-GB",
          timeFormatter: londonTimeFormatter,
        },
        handleScroll: {
          mouseWheel: false,
          pressedMouseMove: interactive,
          horzTouchDrag: interactive,
          vertTouchDrag: false,
        },
        handleScale: {
          mouseWheel: wheelZoom,
          pinch: interactive,
          axisPressedMouseMove: {
            time: interactive,
            price: interactive,
          },
          axisDoubleClickReset: true,
        },
      })

      const seriesOptions = {
        priceFormat: {
          type: "price",
          precision: Number(data.pricePrecision ?? 4),
          minMove: Number(data.minMove ?? 0.0001),
        },
        priceLineVisible: true,
        lastValueVisible: true,
      }

      let priceSeries
      if (data.chartType === "line") {
        priceSeries = chart.addSeries(LWC.LineSeries, {
          ...seriesOptions,
          color: "#4f8cff",
          lineWidth: 2,
          crosshairMarkerVisible: true,
        })
      } else {
        priceSeries = chart.addSeries(LWC.CandlestickSeries, {
          ...seriesOptions,
          upColor: "#16a085",
          downColor: "#e05260",
          wickUpColor: "#16a085",
          wickDownColor: "#e05260",
          borderVisible: false,
        })
      }
      priceSeries.setData(data.priceData ?? [])

      let volumeSeries = null
      if (data.showVolume) {
        volumeSeries = chart.addSeries(
          LWC.HistogramSeries,
          {
            priceFormat: { type: "volume" },
            priceScaleId: "",
            lastValueVisible: false,
            priceLineVisible: false,
          },
          1
        )
        volumeSeries.setData(data.volumeData ?? [])
        volumeSeries.priceScale().applyOptions({
          scaleMargins: { top: 0.12, bottom: 0 },
        })
        const panes = chart.panes?.() ?? []
        if (panes[1]?.setHeight) panes[1].setHeight(Math.max(110, Math.floor(Number(data.chartHeight) * 0.22)))
      }

      const hoverMap = new Map((data.hoverBars ?? []).map((bar) => [String(bar.time), bar]))

      const formatPrice = (value) => {
        const number = Number(value)
        if (!Number.isFinite(number)) return "—"
        return number.toLocaleString(undefined, {
          minimumFractionDigits: 0,
          maximumFractionDigits: Number(data.pricePrecision ?? 6),
        })
      }
      const formatVolume = (value) => {
        const number = Number(value)
        if (!Number.isFinite(number)) return "—"
        return number.toLocaleString(undefined, { maximumFractionDigits: 0 })
      }
      const updateReadout = (bar) => {
        if (!bar) return
        readout.textContent =
          `${bar.label}   O ${formatPrice(bar.open)}` +
          `   H ${formatPrice(bar.high)}` +
          `   L ${formatPrice(bar.low)}` +
          `   C ${formatPrice(bar.close)}` +
          `   V ${formatVolume(bar.volume)}`
      }

      const lastBar = (data.hoverBars ?? [])[Math.max(0, (data.hoverBars ?? []).length - 1)]
      if (lastBar) updateReadout(lastBar)

      chart.subscribeCrosshairMove((param) => {
        if (!param?.time) return
        const bar = hoverMap.get(String(param.time))
        if (bar) updateReadout(bar)
      })

      const resetChart = () => {
        chart?.timeScale?.().fitContent?.()
        try {
          priceSeries?.priceScale?.().applyOptions?.({ autoScale: true })
        } catch (error) {
          // No-op; fitContent already provides a safe reset.
        }
      }
      resetButton.onclick = resetChart

      chart.timeScale().fitContent()

      resizeObserver = new ResizeObserver(() => {
        if (!chart || destroyed) return
        // autoSize handles the dimensions; this keeps fullscreen transitions snappy.
        chart.applyOptions({ autoSize: true })
      })
      resizeObserver.observe(shell)

      const onFullscreenResize = () => {
        if (!chart || destroyed) return
        ownerWindow?.requestAnimationFrame?.(() => chart.applyOptions({ autoSize: true }))
      }
      ownerDocument.addEventListener("fullscreenchange", onFullscreenResize)
      fullscreenCleanup = () => ownerDocument.removeEventListener("fullscreenchange", onFullscreenResize)
    } catch (error) {
      console.error(error)
      showError(
        "TradingView Lightweight Charts could not load. The chart uses a browser CDN (unpkg/jsDelivr) and requires network access to one of them. " +
        String(error?.message ?? error)
      )
    }
  })()

  return destroy
}
"""


_TRADINGVIEW_CHART = st.components.v2.component(
    "qh_tradingview_lightweight_chart_v1",
    html=_COMPONENT_HTML,
    css=_COMPONENT_CSS,
    js=_COMPONENT_JS,
)



def render_ohlc_chart(
    frame: pd.DataFrame,
    *,
    chart_type: str,
    show_volume: bool,
    compress_gaps: bool,
    interval: str,
    navigation_mode: str,
    height: int,
    chart_theme: str = "Dark",
    key: str = "quoted_ohlc_chart",
) -> None:
    """Render QH OHLC using TradingView Lightweight Charts instead of Plotly."""
    if frame.empty:
        return

    times = epoch_seconds(frame["strategy_time_london"])
    if len(times) != len(set(times)):
        raise ValueError(
            "TradingView chart requires one OHLC observation per timestamp, but duplicate "
            "strategy timestamps were found. The app did not aggregate or discard them."
        )

    precision, min_move = price_precision(frame)
    chart_type = chart_type.lower()

    if chart_type == "line":
        price_data: list[dict[str, Any]] = [
            {"time": time, "value": float(close)}
            for time, close in zip(times, frame["close"])
        ]
    else:
        price_data = [
            {
                "time": time,
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
            }
            for time, open_, high, low, close in zip(
                times, frame["open"], frame["high"], frame["low"], frame["close"]
            )
        ]

    volume_data = [
        {
            "time": time,
            "value": float(volume),
            "color": "rgba(22,160,133,0.72)" if close >= open_ else "rgba(224,82,96,0.72)",
        }
        for time, volume, open_, close in zip(times, frame["volume"], frame["open"], frame["close"])
    ]

    if not compress_gaps:
        price_data = with_whitespace(price_data, times=times, interval=interval)
        volume_data = with_whitespace(volume_data, times=times, interval=interval)

    hover_bars = [
        {
            "time": time,
            "label": pd.Timestamp(strategy).strftime("%d %b %Y %H:%M:%S %Z"),
            "open": float(open_),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": float(volume),
        }
        for time, strategy, open_, high, low, close, volume in zip(
            times,
            frame["strategy_time_london"],
            frame["open"],
            frame["high"],
            frame["low"],
            frame["close"],
            frame["volume"],
        )
    ]

    expected = interval_to_timedelta(interval)
    seconds_visible = bool(expected is not None and expected < pd.Timedelta(minutes=1))

    _TRADINGVIEW_CHART(
        key=f"{key}_lwc_v1_{chart_theme.lower()}_{navigation_mode}_{compress_gaps}",
        data={
            "priceData": price_data,
            "volumeData": volume_data,
            "hoverBars": hover_bars,
            "chartType": chart_type,
            "showVolume": bool(show_volume),
            "navigationMode": navigation_mode,
            "theme": chart_theme.lower(),
            "chartHeight": int(height),
            "pricePrecision": int(precision),
            "minMove": float(min_move),
            "secondsVisible": seconds_visible,
            "primaryCdn": _LWC_PRIMARY,
            "fallbackCdn": _LWC_FALLBACK,
        },
        width="stretch",
        height=height,
    )
