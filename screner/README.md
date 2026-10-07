# Screner — QH OHLC + Z-Score Strategy + Lightstreamer Live Data

Screner is a Streamlit research app for QH quoted curve structures such as `COX26-Z26-F27`.

This build contains three connected pieces:

1. **QH historical OHLC** for charting and backtesting.
2. **Stateful Z-score strategy** with scale-in/scale-out tranches and per-tranche price stops.
3. **Lightstreamer live market data** using the AdminPrice feed supplied for this project.

The app is still a market-data/research/signal application. It **does not send exchange orders**.

## Live Lightstreamer integration

Select **QH API + Live** in the sidebar.

The app then uses:

```text
Lightstreamer endpoint: https://ls-md.corp.hertshtengroup.com
Adapter set:           AdminPriceAdapter
Data adapter:          AdminPrice_DataAdapter
User:                  Test
Snapshot:              yes
Requested max freq:    1 update/sec per item
```

The subscribed fields match the supplied JavaScript:

```text
key, command, AdminPrice, AskPrice, AskSize, BidPrice, BidSize, Custom,
LastPrice, LastSize, MP, TotalTradedVolume, WAP, PreviousSettlement,
CurrentSettlement, Timestamp, InvokeTimestamp, FormulaType
```

Contract IDs are resolved through:

```text
https://insight-x.corp.hertshtengroup.com/api/v1/insight/getPDSInstruments
```

with `strategies: ["OUT"]`. The app tries the QH product name first; for `CO` it also tries `BRN` because the supplied JavaScript normalizes `BRN` to `CO`.

### Important live-price distinction

The supplied contract-discovery code requests **OUT contracts**. Therefore a live fly/spread/DFly is calculated from the live outright legs using the selected coefficients. For example:

```text
COX26-Z26-F27 -> 1*COX26 - 2*COZ26 + 1*COF27
```

That Lightstreamer structure is labelled **synthetic/indicative** in the app.

The historical backtest remains on the **direct QH quoted structure**. Do not assume the synthetic Lightstreamer bid/ask is identical to a directly quoted exchange structure market.

### Live panel

The live tab shows:

- Lightstreamer connection state
- age of the latest update
- live structure mark price
- synthetic bid and ask from the outright legs
- time-aligned live Z-score for the developing selected-timeframe candle
- frozen rolling benchmark mean and SD for that candle
- candle start / close time / countdown
- developing live candle OHLC
- completed Lightstreamer candles
- intrabar BUY/SELL entry and exit signals
- intrabar signal-state position and active tranches
- completed-candle model position for backtest comparison
- next Z entry/exit actions with equivalent structure trigger prices
- all live outright leg prices/sizes/volume/timestamps

You can choose the mark field used to build the structure:

- `AdminPrice`
- `LastPrice`
- `MP`
- `Mid`
- `WAP`

`AdminPrice` is the default. A selected field has sensible fallbacks when a leg has no value yet.

### Live candle rules

Lightstreamer ticks are aggregated into the same selected strategy interval, e.g. `1M`, `5M`, `15M`, `1H`.

- Missing intervals are **not forward-filled**.
- A candle exists only if at least one live structure tick was observed.
- The first bucket after connecting may be partial if the feed starts mid-interval. Screner excludes that first bucket from strategy history unless the first tick arrives within two seconds of the interval boundary.
- The developing candle does not enter its own rolling Z benchmark.
- LIVE Z can update every second, but every update remains an observation of the **same developing selected-timeframe candle**, not a new one-second strategy candle.
- The benchmark mean/SD stays locked for the duration of that candle and rolls only at its boundary.
- Intrabar signal processing uses the displayed live-Z sequence, so asynchronous individual outright-leg callbacks are not treated as separate strategy candles.
- Completed live candles are appended only after the final QH historical strategy timestamp.

## Z-score strategy

Default short logic:

| Tranche | Entry | Qty | Z exit | Stop loss |
|---|---:|---:|---:|---:|
| 1 | +1.50 upward crossing | Sell 1 | +1.00 downward crossing | Editable |
| 2 | +2.00 upward crossing | Sell 1 | +1.50 downward crossing | Editable |
| 3 | +2.50 upward crossing | Sell 2 | +2.00 downward crossing | Editable |

Longs are mirrored at negative Z values.

### Rolling Z benchmark

At candle `t`, mean and standard deviation use only the previous `X` completed real candles. The current candle is standardized against that frozen benchmark and does not enter its own mean/SD calculation.

The live tab now treats LIVE Z explicitly as the Z-score of the **developing selected-timeframe candle**. For example, when `5M` is selected, the displayed price is the changing close of the current 5-minute candle and its mean/SD benchmark is frozen from the previous `X` completed 5-minute candles. The benchmark changes only when the selected candle closes.

There are two signal-timing modes:

- **Intrabar live** (default): the app processes one displayed live-Z observation per refresh and emits BUY/SELL entry or exit events immediately when Z crosses the configured threshold. The intrabar signal state is persistent, so an entry can later generate an exit inside the same 5-minute/15-minute/etc. candle. The latest signal remains visible in the panel.
- **Candle close only**: LIVE Z still moves, but entries/exits are delayed until the selected candle closes, matching the historical backtest logic.

The intrabar signal state and completed-candle backtest state are shown separately on purpose. Neither sends exchange orders.

## Per-tranche stop loss

Every tranche has its own adverse price distance in ticks:

```text
SHORT stop = entry price + stop ticks * tick size
LONG stop  = entry price - stop ticks * tick size
```

A stop value of `0` disables the stop.

Historical stop tests use the following bar's high/low. Gap-through fills use the open, and a stop is prioritized over a Z exit if both could have happened inside the same OHLC bar.

## Backtest output

The strategy lab reports:

- net P&L in ticks
- win rate
- average winner / loser
- max drawdown
- max absolute position
- stop-loss exit count and P&L
- Z-exit count and P&L
- transaction costs
- equity curve
- completed-tranche table
- signal/event log
- Z-score audit

## QH access

For **QH API** or **QH API + Live**:

1. Open QH Microsoft sign-in from the sidebar.
2. Sign in.
3. Paste the returned access token or auth JSON into Screner.
4. Click **Save & use access token**.

The extracted token is saved locally to:

```text
.secrets/qh_authorization.txt
```

## Windows PowerShell

If `screner_zscore_live_lightstreamer.zip` is in Downloads:

```powershell
$downloads = "$HOME\Downloads"
$zip = "$downloads\screner_zscore_live_lightstreamer.zip"
$dest = "$downloads\screner_zscore_live_lightstreamer_RUN"

if (Test-Path $dest) {
    Remove-Item $dest -Recurse -Force
}

Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
Set-Location "$dest\screner"

python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
& ".\.venv\Scripts\Activate.ps1"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m streamlit run app.py --server.port 8501
```

Open:

```text
http://localhost:8501
```

If another Streamlit app is already using 8501, use another port, for example:

```powershell
python -m streamlit run app.py --server.port 8503
```

## Dependencies

The live build adds the official Lightstreamer Python client:

```text
lightstreamer-client-lib>=2.2.3,<3
```

The app must be run from a machine/network that can reach the corporate Insight X and `ls-md` endpoints.

## Project layout

```text
app.py                 Streamlit UI, QH controls, Strategy Lab and live monitor
strategy_engine.py     Rolling Z and tranche/stop backtest state machine
live_stream.py         Insight contract discovery + Lightstreamer feed + live candles
qh_client.py           QH historical API client/cache/rate-limit handling
data_utils.py          QH normalization, timestamps and gap audit
chart_component.py     TradingView-style chart component
chart_data.py          Chart helpers
auth_utils.py          QH access-token handling
tests/                 Unit tests
```

## Validation status

The included unit tests cover the existing OHLC/QH/strategy behavior plus live structure parsing, coefficient handling, live synthetic bid/ask calculation, partial live-candle handling, no-forward-fill behavior, appending completed live candles to history, Z-to-price trigger conversion, persistent live threshold status, and intrabar entry/exit state transitions.

The corporate Lightstreamer and Insight X endpoints are not reachable from the build environment, so the actual corporate connection must be verified on your Windows machine. The UI reports Lightstreamer status, subscription/server errors, missing TT leg IDs, and the latest-update age to make that verification straightforward.

## QH 15M / larger strategy intervals

Some QH deployments do not expose every strategy timeframe as a native API table. For example, the API may return `Invalid table for 15M`.

This build treats `15M` as a strategy timeframe and loads real `5M` QH bars, then aggregates those observed bars locally into 15-minute OHLC candles. It does not forward-fill missing 5-minute bars and it does not create empty 15-minute buckets. `30M` uses the same 5-minute base; `4H` uses 1-hour QH bars. If another requested QH table reports `Invalid table`, the app also has a 5-minute fallback where mathematically possible.

Lightstreamer remains independent of the QH table choice: streaming ticks continue at their native update rate and are bucketed into the selected strategy interval for the developing/completed live candles.
