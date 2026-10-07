"""Read the Insight header-plus-timestamped-prices file without executing JavaScript."""
from __future__ import annotations

import json
import re
import math
import codecs
import heapq
import time
from collections import OrderedDict
from dataclasses import dataclass

import pandas as pd

from data_utils import interval_to_timedelta, prepare_ohlc_data, resample_ohlc_data

DEFAULT_INSIGHT_URL = "https://insight.corp.hertshtengroup.com/technicals/rangebound/historical/alldatahistoricalenergy.js"
_HTTP_CACHE = OrderedDict()


class InsightPriceError(ValueError):
    pass


def parse_insight_text(text: str) -> tuple[list[str], list[list]]:
    text = text.lstrip("\ufeff").strip()
    # Accept a literal JSON array or a single JS variable assignment only.
    text = re.sub(r"^(?:(?:var|let|const)\s+)?[A-Za-z_$][\w$]*\s*=\s*", "", text, count=1)
    text = text.rstrip().removesuffix(";").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InsightPriceError("The response is not the expected price array. It may be a sign-in page.") from exc
    if not isinstance(payload, list) or len(payload) < 2:
        raise InsightPriceError("Expected an instrument header followed by timestamped rows.")
    names = payload[0]
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
        raise InsightPriceError("The first row must contain instrument names.")
    if len(set(names)) != len(names):
        raise InsightPriceError("Duplicate instrument names make column selection ambiguous.")
    rows = payload[1:]
    if len(payload) == 2 and isinstance(payload[1], list) and (not payload[1] or isinstance(payload[1][0], list)):
        rows = payload[1]
    if not all(isinstance(row, list) and len(row) == len(names) + 1 for row in rows):
        raise InsightPriceError("Each price row must have one timestamp plus one value per instrument.")
    return names, rows


def iter_insight_rows(chunks):
    """Decode one top-level row at a time, with bounded row buffering."""
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    parser = json.JSONDecoder()
    chunks = iter(chunks)
    buffer, ended = "", False
    total_bytes = 0

    def more():
        nonlocal buffer, ended, total_bytes
        try:
            chunk = next(chunks)
        except StopIteration:
            buffer += decoder.decode(b"", final=True)
            ended = True
            return False
        total_bytes += len(chunk)
        if total_bytes > 2 * 1024 ** 3:
            raise InsightPriceError("Price file exceeds the 2 GB streaming download limit.")
        buffer += decoder.decode(chunk)
        return True

    while "[" not in buffer and more():
        if "[" not in buffer and len(buffer) > 4096:
            raise InsightPriceError("The response has no price-array opening. It may be an HTML/error page.")
    start = buffer.find("[")
    prefix = buffer[:start].strip() if start >= 0 else buffer
    if start < 0 or (prefix and not re.fullmatch(r"(?:(?:var|let|const)\s+)?[A-Za-z_$][\w$]*\s*=", prefix)):
        raise InsightPriceError("Expected a JSON price array or a single variable assignment.")
    buffer = buffer[start + 1:]

    def whitespace():
        nonlocal buffer
        buffer = buffer.lstrip()
        while not buffer and not ended:
            more()
            buffer = buffer.lstrip()

    def token(expected):
        nonlocal buffer
        whitespace()
        if not buffer.startswith(expected):
            raise InsightPriceError(f"Expected {expected!r} in the price-array structure.")
        buffer = buffer[len(expected):]

    def decode_row():
        nonlocal buffer
        while True:
            buffer = buffer.lstrip()
            try:
                row, consumed = parser.raw_decode(buffer)
                break
            except json.JSONDecodeError as exc:
                if ended or len(buffer) > 8 * 1024 ** 2:
                    raise InsightPriceError("Invalid or truncated price row.") from exc
                more()
        if not isinstance(row, list):
            raise InsightPriceError("Price file rows must be arrays.")
        buffer = buffer[consumed:]
        return row

    # The corporate file is [header, [row, row, ...]]. Also accept the flat
    # [header, row, row, ...] format. Never decode the whole nested history.
    yield decode_row()
    whitespace()
    if buffer.startswith("]"):
        buffer = buffer[1:]
    else:
        token(",")
        whitespace()
        if not buffer.startswith("["):
            raise InsightPriceError("Expected timestamped row arrays after the header.")
        # Look beyond the next opening bracket without consuming a price row.
        while not buffer[1:].lstrip() and not ended:
            more()
        nested = buffer[1:].lstrip().startswith(("[", "]"))
        if nested:
            token("[")
        first = True
        while True:
            whitespace()
            if buffer.startswith("]"):
                buffer = buffer[1:]
                break
            if not first:
                token(",")
            yield decode_row()
            first = False
        if nested:
            token("]")
    # A single terminal semicolon is allowed; other JavaScript is rejected.
    while not ended:
        more()
        if len(buffer) > 4096:
            raise InsightPriceError("Unexpected content after the price array.")
    if buffer.strip() not in ("", ";"):
        raise InsightPriceError("Unexpected content after the price array.")


def select_insight_rows(row_iterator, instrument=None, max_rows=200_000):
    try:
        names = next(row_iterator)
    except StopIteration as exc:
        raise InsightPriceError("Price file is empty.") from exc
    if not names or not all(isinstance(n, str) and n for n in names) or len(set(names)) != len(names):
        raise InsightPriceError("Expected unique instrument names in the first row.")
    if instrument is None:
        # Stop after the header; requests closes the remaining response body.
        return names, []
    if instrument not in names:
        raise InsightPriceError(f"Instrument {instrument!r} is absent from this file.")
    column = names.index(instrument) + 1
    retained = []
    for sequence, row in enumerate(row_iterator):
        if len(row) != len(names) + 1:
            raise InsightPriceError("Each price row must have one timestamp plus one value per instrument.")
        try:
            timestamp = pd.Timestamp(row[0])
            if pd.isna(timestamp):
                raise ValueError()
            key = timestamp.value
        except (ValueError, TypeError, OverflowError) as exc:
            raise InsightPriceError(f"Unrecognized timestamp: {row[0]!r}") from exc
        # Heap preserves the most recent observations even in descending/unsorted files.
        entry = (key, sequence, row[0], row[column])
        if len(retained) < max_rows:
            heapq.heappush(retained, entry)
        elif entry[:2] > retained[0][:2]:
            heapq.heapreplace(retained, entry)
    if not retained:
        raise InsightPriceError("No timestamped rows were found.")
    return [instrument], [[timestamp, price] for _, _, timestamp, price in sorted(retained)]


def fetch_insight(url: str, instrument=None) -> tuple[list[str], list[list]]:
    import requests
    if not url.lower().startswith("https://"):
        raise InsightPriceError("Use an HTTPS price-file URL.")
    cache_key = (url, instrument)
    cached = _HTTP_CACHE.get(cache_key)
    if cached and time.monotonic() - cached["checked"] < 5:
        return cached["payload"]
    headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
    if cached:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("modified"):
            headers["If-Modified-Since"] = cached["modified"]
    try:
        with requests.get(url, timeout=(10, 45), stream=True,
                          headers=headers) as response:
            if response.status_code == 304 and cached:
                cached["checked"] = time.monotonic()
                return cached["payload"]
            response.raise_for_status()
            payload = select_insight_rows(iter_insight_rows(response.iter_content(64 * 1024)), instrument)
            _HTTP_CACHE[cache_key] = {"payload": payload, "checked": time.monotonic(),
                                     "etag": response.headers.get("ETag"),
                                     "modified": response.headers.get("Last-Modified")}
            _HTTP_CACHE.move_to_end(cache_key)
            while len(_HTTP_CACHE) > 2:
                _HTTP_CACHE.popitem(last=False)
            return payload
    except requests.exceptions.SSLError as exc:
        raise InsightPriceError(
            "Windows could not validate the site's certificate. Confirm that the corporate "
            "root/intermediate certificates are installed by IT and restart the app. "
            "Certificate verification is enabled."
        ) from exc
    except (requests.RequestException, UnicodeDecodeError) as exc:
        raise InsightPriceError(f"Cannot load the Insight file: {exc}. Check corporate network/VPN access.") from exc


@dataclass
class InsightSnapshot:
    completed: pd.DataFrame
    developing: pd.DataFrame
    latest_time: pd.Timestamp
    latest_price: float
    skipped: int
    age_seconds: float


def build_insight_snapshot(names, rows, instrument, interval, *, missing_value=-100.0,
                           naive_timezone="UTC", offset_minutes=0, now=None):
    if instrument not in names:
        raise InsightPriceError(f"Instrument {instrument!r} is absent from this file.")
    duration = interval_to_timedelta(interval)
    if duration is None:
        raise InsightPriceError("Choose a supported time-based candle interval.")
    index = names.index(instrument) + 1
    observations, skipped = [], 0
    for row in rows:
        raw_price = row[index]
        try:
            if raw_price is None or isinstance(raw_price, bool):
                raise ValueError()
            price = float(raw_price)
            if not math.isfinite(price) or (missing_value is not None and price == missing_value):
                raise ValueError()
        except (ValueError, TypeError):
            skipped += 1
            continue
        try:
            timestamp = pd.Timestamp(row[0])
            if pd.isna(timestamp):
                raise ValueError()
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize(naive_timezone, ambiguous="raise", nonexistent="raise")
            timestamp = timestamp.tz_convert("UTC")
        except Exception as exc:
            raise InsightPriceError(f"Unrecognized timestamp: {row[0]!r}") from exc
        observations.append({"time": timestamp.value // 1_000_000, "product": instrument,
                             "open": price, "high": price, "low": price, "close": price, "volume": 0})
    if not observations:
        raise InsightPriceError("No usable prices for this instrument after missing-value filtering.")
    samples = prepare_ohlc_data(observations, timestamp_offset_minutes=offset_minutes, instrument=instrument)
    samples = samples.drop_duplicates("strategy_time_london", keep="last").reset_index(drop=True)
    bars = resample_ohlc_data(samples, interval)
    current = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now).tz_convert("UTC")
    latest = samples.iloc[-1]
    latest_time = latest["strategy_time_london"].tz_convert("UTC")
    age = float((current - latest_time).total_seconds())
    ends = bars["strategy_time_london"].dt.tz_convert("UTC") + duration
    completed = bars.loc[ends <= current].reset_index(drop=True)
    developing = bars.loc[(ends > current) & (bars["strategy_time_london"].dt.tz_convert("UTC") <= current)].reset_index(drop=True)
    return InsightSnapshot(completed, developing, latest_time, float(latest["close"]), skipped, age)
