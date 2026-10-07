# Direct structure OHLC

Select **QH API + Live** and enter **COF27-G27-H27**. Use the QH access panel
to save your Microsoft-issued access token, choose **5M**, and set **Maximum bars**
to **500**. Open **Live prices**. The default polling interval is 5 seconds.

Auto refresh is available in both **QH API** and **QH API + Live**. Enable
**Auto refresh QH charts and results** in the sidebar. A single dashboard timer
updates the chart, strategy results, price panel and timestamp audit together.
The chart includes a developing candle when supplied by QH; backtests use
completed candles. A visible refresh timestamp reports each dashboard update.

The client limiter uses the documented 30 OHLC requests/minute. The five-second
default normally makes 12 requests/minute; the DFly component flies are batched
into one request, with an occasional additional direct-DFly availability check.
The price panel reuses the dashboard's short-lived data cache. HTTP 429 retry
and backoff still apply when other apps or server limits consume the allowance.

## Z-score on the chart page

The **Price / OHLC** page displays a Z-score panel immediately above the chart.
Use **Z-score lookback (candles)** to select the number of preceding completed
candles (default 40, range 2–5,000). This setting is shared with Strategy Lab
and live signals. Changing the selected candle interval changes the source
candles used for every calculation: 40 at 5M uses 40 five-minute candles, and
40 at 1H uses 40 hourly candles.

The panel calculates `(latest close − prior mean) / prior population SD`.
The latest candle never enters its own benchmark. The benchmark is frozen while
a candle develops and changes at the next candle or when interval/lookback/source
changes. Missing market intervals are not filled. If insufficient candles are
loaded or SD is zero, the panel explains why the score is unavailable.

When QH provides a developing candle, the label is **LIVE Z**. Otherwise it
shows **Latest candle Z**, with the actual candle timestamp and a stale-data
notice when appropriate. The existing five-second dashboard timer updates it.
Five additional tests verified the formula, frozen benchmark, interval selection,
unavailable-data cases and input changes in the running application.

The request is:

```http
GET https://qh-api.corp.hertshtengroup.com/apis/ohlc/?instruments=COF27-G27-H27&interval=5M&count=500
Authorization: Bearer <access_token>
```

The current QH live mode uses the OHLC values returned for the exact structure.
It does not start Lightstreamer, resolve outright legs or calculate a synthetic
fly. Responses labelled with a different instrument are rejected. The QH
backend's own price-construction methodology is outside this app's control.

## DFly support

For `COF27-G27-H27-J27`, the app first requests that exact instrument. If QH
returns no recognizable OHLC records, it fetches these two quoted flies in a
single API request:

```text
instruments=COF27-G27-H27,COG27-H27-J27
```

The DFly price is the first fly minus the second: `F − 3G + 3H − J`.
Only matching candle timestamps are used. Missing component candles are never
filled across gaps. An unavailable direct DFly is remembered for 60 seconds
to avoid repeating the failed direct request on every live poll.

Derived opens and closes are differences of the component opens and closes.
Independent fly OHLC bars cannot reconstruct the DFly's actual intrabar highs
or lows. The displayed high/low range therefore covers the calculated endpoints
only; traded DFly volume is unavailable. The app labels this explicitly and
disables price-stop backtests until each stop setting is zero. Z-score research
uses calculated closes. No outright prices are fetched to derive the DFly.

On 7 October 2026 the authenticated QH check produced 369 matched 5-minute
candles for this example. Its latest corrected candle time was 12:05 BST, with
a calculated close of 0.28. Nine focused tests, including application tests with
mocked QH responses, passed. The data source may have missing/stale candles;
the live panel continues to apply its freshness checks.

15M and 30M intervals aggregate 5M QH candles for the same exact structure;
4H aggregates its 1H candles. No outright prices are used in that aggregation.

Polling requests the latest available candles. Whether QH returns developing
candles, and how often their OHLC changes, depends on the service. Candle-close
mode is the default. Intrabar mode only acts when a developing candle is present.
The existing intrabar tracker does not implement live stop-loss exits.

The existing editable 65-minute QH timestamp correction is preserved. Raw and
corrected timestamps are displayed together. Confirm the timestamp convention
with QH before altering the correction; the supplied screenshot shows a raw
timestamp one hour ahead of its HTTP response time. Stale/future candle times
block new intrabar signals. Charts include any developing candle supplied by
QH; backtests use completed candles. The QH dashboard refreshes as one unit.

Validation: ten focused tests, including Streamlit AppTests using mocked
QH response, verified the exact structure request, OHLC preservation, rejection
of the wrong instrument, same-structure timeframe aggregation and candle timing.
A refresh test verifies the five-second timer, updated chart prices and exclusion
of the developing candle from backtests in the ordinary QH API mode.
The authenticated DFly check is recorded above.
