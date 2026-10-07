# Insight file price source

Run `run_app.bat`, then select **Insight file (polling)** under **Data source**.
The supplied corporate URL is prefilled. Select an exact instrument from the file,
choose a candle interval, and open **Live prices**. The panel polls every 15 seconds
by default; the chart and Strategy Lab refresh on a full page rerun.

The parser expects the format visible in the supplied screenshot:

```json
[["INSTRUMENT_A", "INSTRUMENT_B"], [
 ["2026-10-06T10:00:00.000000000Z", 1.25, -100],
 ["2026-10-06T10:01:00.000000000Z", 1.30, 0.50]]]
```

It also accepts a flat header-plus-rows layout and a single JavaScript variable assignment containing that JSON.
The loader reads 64 KB chunks and retains only the selected column's most recent
200,000 observations. Instrument discovery reads only the header. The previous
64 MB full-file limit is removed; the streaming download ceiling is 2 GB and
an individual row is limited to 8 MB. ETag/Last-Modified conditional requests
avoid repeated full downloads when supported by the server. Without those
headers the entire remote file must still be downloaded on each poll.
It never executes JavaScript. Unexpected column counts or duplicate instrument
names fail explicitly. No CO/LCO or contract-year substitutions are made.

The repeated `-100` values are treated as missing by default, based on the
screenshot. This assumption is configurable; confirm the source convention.
Other negative prices remain valid. Null and nonfinite prices are omitted.

Source timestamps with `Z` are UTC. For timestamps without a timezone, choose
UTC or Europe/London. The default timestamp correction for this source is zero;
it does not inherit the original QH 65-minute correction.

Candles are built from observed file prices. They do not contain exchange OHLC
extremes, traded volume or bid/ask. Missing intervals are not filled. Developing
candles are excluded from the historical backtest and its rolling benchmark.
The intrabar benchmark is frozen during each candle.

Prices older than the configured limit (180 seconds by default), or more than
5 seconds in the future, block new intrabar signals. Stale prices remain visible
with their timestamp. Network/parser failures stop signal processing. Historical
candle-close event tables remain model history, not new real-time alerts.

Polling can miss intermediate crossings. The latest file timestamp, not the time
of download, determines freshness. A file with old historical rows will not turn
into a live feed merely because it is polled. The screenshot shows May 2026 rows;
the end of the actual file must contain current observations for live use.

The existing intrabar engine stores stop levels but does not generate live
stop-loss exits. The historical engine supports OHLC stop checks; for this source,
those checks only cover observed prices and can miss actual intraperiod extremes.
No exchange orders are placed.

Corporate network/VPN access is required. Python requests may not share browser
sign-in sessions. TLS validation remains enabled. Credentials, saved tokens,
caches and the original virtual environment are excluded from this package.

Validation: 16 offline tests cover parser/chunk boundaries, nested history,
candle aggregation, missing values, timestamp correction, stale gating and
panel/strategy integration. On 6 October 2026, the actual corporate response was
successfully parsed: 370 instruments, 150,000 rows, 307,067,411 decompressed bytes.
For LCOX6-Z6, 85,905 usable observations produced 25,088 completed 5-minute
candles. Its latest valid observation was 30 September 2026 at 17:36 UTC,
price 5.235. That observation is stale on the verification date. Other symbols
can have different last valid observations; the app checks each selection.
