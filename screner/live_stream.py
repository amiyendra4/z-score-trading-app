from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
import threading
from typing import Any, Iterable

import pandas as pd
import requests


LS_ENDPOINT = "https://ls-md.corp.hertshtengroup.com"
LS_ADAPTER_SET = "AdminPriceAdapter"
LS_DATA_ADAPTER = "AdminPrice_DataAdapter"
INSIGHT_CONTRACTS_URL = (
    "https://insight-x.corp.hertshtengroup.com/api/v1/insight/getPDSInstruments"
)

LIVE_FIELDS = [
    "key",
    "command",
    "AdminPrice",
    "AskPrice",
    "AskSize",
    "BidPrice",
    "BidSize",
    "Custom",
    "LastPrice",
    "LastSize",
    "MP",
    "TotalTradedVolume",
    "WAP",
    "PreviousSettlement",
    "CurrentSettlement",
    "Timestamp",
    "InvokeTimestamp",
    "FormulaType",
]

_MONTH_CODE_RE = re.compile(r"^[FGHJKMNQUVXZ]\d{2,4}$", re.IGNORECASE)
_FIRST_LEG_RE = re.compile(r"^([A-Z]+?)([FGHJKMNQUVXZ]\d{2,4})$", re.IGNORECASE)


class LiveFeedError(RuntimeError):
    """Safe user-facing error for live market-data setup."""


@dataclass(frozen=True)
class StructureDefinition:
    qh_code: str
    product: str
    month_codes: tuple[str, ...]
    coefficients: tuple[float, ...]

    @property
    def expression(self) -> str:
        return " + ".join(
            f"{coef:g}*{self.product}{month}" for coef, month in zip(self.coefficients, self.month_codes)
        ).replace("+ -", "- ")


@dataclass(frozen=True)
class LiveResolution:
    definition: StructureDefinition
    item_names: tuple[str, ...]
    qh_leg_codes: tuple[str, ...]


@dataclass(frozen=True)
class LiveTick:
    received_at_utc: datetime
    price: float
    bid: float | None
    ask: float | None


def default_coefficients(leg_count: int) -> tuple[float, ...]:
    """Infer the standard curve structure weights from the number of quoted legs."""
    defaults: dict[int, tuple[float, ...]] = {
        1: (1.0,),
        2: (1.0, -1.0),
        3: (1.0, -2.0, 1.0),
        4: (1.0, -3.0, 3.0, -1.0),
    }
    if leg_count not in defaults:
        raise LiveFeedError(
            f"Cannot infer coefficients for {leg_count} legs. Enter the live coefficients manually."
        )
    return defaults[leg_count]


def parse_coefficients(value: str | Iterable[float], leg_count: int) -> tuple[float, ...]:
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
        try:
            coefficients = tuple(float(part) for part in parts)
        except ValueError as exc:
            raise LiveFeedError("Live coefficients must be comma-separated numbers, e.g. 1,-2,1.") from exc
    else:
        try:
            coefficients = tuple(float(part) for part in value)
        except (TypeError, ValueError) as exc:
            raise LiveFeedError("Live coefficients must be numeric.") from exc
    if len(coefficients) != leg_count:
        raise LiveFeedError(
            f"The quoted code has {leg_count} live legs but {len(coefficients)} coefficient(s) were supplied."
        )
    if not coefficients or any(not math.isfinite(item) for item in coefficients):
        raise LiveFeedError("Live coefficients must all be finite numbers.")
    if all(abs(item) < 1e-12 for item in coefficients):
        raise LiveFeedError("At least one live coefficient must be non-zero.")
    return coefficients


def parse_quoted_structure(
    instrument: str,
    coefficients: str | Iterable[float] | None = None,
) -> StructureDefinition:
    code = str(instrument).strip().upper()
    if not code:
        raise LiveFeedError("Enter a quoted QH structure before connecting live data.")

    parts = [part.strip() for part in code.split("-") if part.strip()]
    if not parts:
        raise LiveFeedError("Could not parse the quoted structure code.")

    first = _FIRST_LEG_RE.fullmatch(parts[0])
    if not first:
        raise LiveFeedError(
            "Live parsing expects a QH code like CLX26, CLX26-Z26, or COX26-Z26-F27."
        )
    product = first.group(1).upper()
    month_codes: list[str] = [first.group(2).upper()]

    for raw_part in parts[1:]:
        part = raw_part.upper()
        if _MONTH_CODE_RE.fullmatch(part):
            month_codes.append(part)
            continue
        full = _FIRST_LEG_RE.fullmatch(part)
        if full and full.group(1).upper() == product:
            month_codes.append(full.group(2).upper())
            continue
        raise LiveFeedError(
            f"Could not parse live leg '{raw_part}'. Expected a month code such as Z26."
        )

    inferred = default_coefficients(len(month_codes)) if coefficients is None else parse_coefficients(coefficients, len(month_codes))
    return StructureDefinition(
        qh_code=code,
        product=product,
        month_codes=tuple(month_codes),
        coefficients=tuple(inferred),
    )


def coefficients_text_for_instrument(instrument: str) -> str:
    try:
        definition = parse_quoted_structure(instrument)
    except LiveFeedError:
        return ""
    return ",".join(f"{value:g}" for value in definition.coefficients)


def _candidate_discovery_products(product: str) -> tuple[str, ...]:
    product = product.upper()
    # QH commonly labels Brent as CO while Insight/PDS may label it BRN. Try the
    # exact product first, then BRN as a fallback for CO.
    return ("CO", "BRN") if product == "CO" else (product,)


def discover_out_contracts(
    product: str,
    *,
    endpoint: str = INSIGHT_CONTRACTS_URL,
    timeout_seconds: int = 20,
    session: requests.Session | None = None,
) -> dict[str, str]:
    """Return {month_code: TT-item-name} using the Insight X contract endpoint."""
    requester = session or requests.Session()
    errors: list[str] = []
    for request_product in _candidate_discovery_products(product):
        try:
            response = requester.post(
                endpoint,
                json={"products": [request_product], "strategies": ["OUT"]},
                headers={"Content-Type": "application/json"},
                timeout=timeout_seconds,
            )
        except requests.RequestException as exc:
            errors.append(f"{request_product}: {exc}")
            continue

        if response.status_code >= 400:
            errors.append(
                f"{request_product}: HTTP {response.status_code} {response.text[:160].strip()}"
            )
            continue
        try:
            payload = response.json()
        except requests.JSONDecodeError:
            errors.append(f"{request_product}: invalid JSON response")
            continue

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            errors.append(f"{request_product}: response had no contract data list")
            continue

        mapping: dict[str, str] = {}
        for row in data:
            if not isinstance(row, dict):
                continue
            instid = row.get("instid")
            qh_code = str(row.get("qh_code") or "").strip().upper()
            if instid is None or not qh_code:
                continue
            month_match = re.search(r"([FGHJKMNQUVXZ]\d{2,4})$", qh_code)
            if not month_match:
                continue
            month_code = month_match.group(1).upper()
            item_name = str(instid).strip()
            if not item_name.startswith("TT-"):
                item_name = "TT-" + item_name
            mapping[month_code] = item_name

        if mapping:
            return mapping
        errors.append(f"{request_product}: no OUT contracts returned")

    detail = "; ".join(errors[-3:]) if errors else "no response details"
    raise LiveFeedError(f"Insight X returned no usable OUT contracts for {product}. {detail}")


def resolve_structure_items(
    instrument: str,
    coefficients: str | Iterable[float] | None = None,
    *,
    endpoint: str = INSIGHT_CONTRACTS_URL,
    session: requests.Session | None = None,
) -> LiveResolution:
    definition = parse_quoted_structure(instrument, coefficients)
    mapping = discover_out_contracts(definition.product, endpoint=endpoint, session=session)
    missing = [month for month in definition.month_codes if month not in mapping]
    if missing:
        raise LiveFeedError(
            "Insight X could not resolve live TT IDs for: " + ", ".join(missing)
        )
    items = tuple(mapping[month] for month in definition.month_codes)
    qh_legs = tuple(f"{definition.product}{month}" for month in definition.month_codes)
    return LiveResolution(definition=definition, item_names=items, qh_leg_codes=qh_legs)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def quote_price(quote: dict[str, Any], source: str) -> float | None:
    """Extract a robust mark price from one live outright quote."""
    bid = _as_float(quote.get("BidPrice"))
    ask = _as_float(quote.get("AskPrice"))
    midpoint = (bid + ask) / 2.0 if bid is not None and ask is not None else None

    source = str(source or "AdminPrice")
    priorities: dict[str, tuple[str, ...]] = {
        "AdminPrice": ("AdminPrice", "LastPrice", "MP", "WAP"),
        "LastPrice": ("LastPrice", "AdminPrice", "MP", "WAP"),
        "MP": ("MP", "AdminPrice", "LastPrice", "WAP"),
        "WAP": ("WAP", "AdminPrice", "LastPrice", "MP"),
    }
    if source == "Mid":
        if midpoint is not None:
            return midpoint
        order = ("MP", "AdminPrice", "LastPrice", "WAP")
    else:
        order = priorities.get(source, priorities["AdminPrice"])

    for field in order:
        value = _as_float(quote.get(field))
        if value is not None:
            return value
    return midpoint


def compute_structure_snapshot(
    quotes: dict[str, dict[str, Any]],
    item_names: Iterable[str],
    coefficients: Iterable[float],
    *,
    price_source: str = "AdminPrice",
) -> dict[str, float | None] | None:
    items = tuple(item_names)
    coeffs = tuple(float(value) for value in coefficients)
    if len(items) != len(coeffs) or not items:
        return None

    marks: list[float] = []
    bids: list[float] = []
    asks: list[float] = []
    all_executable = True
    for item, coefficient in zip(items, coeffs):
        quote = quotes.get(item, {})
        mark = quote_price(quote, price_source)
        if mark is None:
            return None
        marks.append(coefficient * mark)

        bid = _as_float(quote.get("BidPrice"))
        ask = _as_float(quote.get("AskPrice"))
        if bid is None or ask is None:
            all_executable = False
        else:
            # Selling the structure: positive legs sell at bid and negative legs
            # buy at ask. Buying the structure is the mirror image.
            bids.append(coefficient * (bid if coefficient >= 0 else ask))
            asks.append(coefficient * (ask if coefficient >= 0 else bid))

    result: dict[str, float | None] = {
        "price": float(sum(marks)),
        "bid": float(sum(bids)) if all_executable else None,
        "ask": float(sum(asks)) if all_executable else None,
    }
    if result["bid"] is not None and result["ask"] is not None:
        result["spread"] = float(result["ask"] - result["bid"])
    else:
        result["spread"] = None
    return result


def build_live_candles(
    ticks: pd.DataFrame,
    interval: str,
    *,
    now_utc: datetime | pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate received live structure ticks into completed and developing OHLC bars.

    This never forward-fills missing intervals. A bucket exists only when at least one
    live structure tick was actually received in that bucket.
    """
    from data_utils import interval_to_timedelta

    expected = interval_to_timedelta(interval)
    columns = ["bucket_utc", "strategy_time_london", "open", "high", "low", "close", "live_tick_count"]
    if expected is None or expected <= pd.Timedelta(0):
        raise LiveFeedError(f"Live candle building does not understand interval '{interval}'.")
    if ticks is None or ticks.empty:
        empty = pd.DataFrame(columns=columns)
        return empty.copy(), empty.copy()

    frame = ticks.copy()
    if "received_at_utc" not in frame or "price" not in frame:
        raise LiveFeedError("Live tick history is missing received_at_utc/price fields.")
    frame["received_at_utc"] = pd.to_datetime(frame["received_at_utc"], utc=True, errors="coerce")
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    frame = frame.dropna(subset=["received_at_utc", "price"]).sort_values("received_at_utc")
    if frame.empty:
        empty = pd.DataFrame(columns=columns)
        return empty.copy(), empty.copy()

    interval_ns = int(expected.value)
    timestamps_ns = frame["received_at_utc"].astype("int64")
    frame["bucket_utc"] = pd.to_datetime((timestamps_ns // interval_ns) * interval_ns, utc=True)

    grouped = frame.groupby("bucket_utc", sort=True)["price"]
    candles = grouped.agg(open="first", high="max", low="min", close="last", live_tick_count="size").reset_index()
    candles["strategy_time_london"] = candles["bucket_utc"].dt.tz_convert("Europe/London")

    current = pd.Timestamp(now_utc or datetime.now(timezone.utc))
    if current.tzinfo is None:
        current = current.tz_localize("UTC")
    else:
        current = current.tz_convert("UTC")
    current_ns = int(current.value)
    current_bucket = pd.Timestamp((current_ns // interval_ns) * interval_ns, tz="UTC")

    completed_mask = candles["bucket_utc"] < current_bucket
    # The feed can be connected in the middle of an interval. Such a first bucket
    # does not have the real interval open/high/low, so do not promote it to a
    # strategy candle unless the first tick arrived within two seconds of the boundary.
    first_tick = frame["received_at_utc"].iloc[0]
    first_bucket = candles["bucket_utc"].iloc[0]
    if (first_tick - first_bucket).total_seconds() > 2.0:
        completed_mask &= candles["bucket_utc"] != first_bucket

    completed = candles.loc[completed_mask].reset_index(drop=True)
    developing = candles.loc[candles["bucket_utc"] >= current_bucket].reset_index(drop=True)
    return completed[columns], developing[columns]


def append_live_candles_to_history(
    history: pd.DataFrame,
    live_completed: pd.DataFrame,
    instrument: str,
) -> pd.DataFrame:
    """Append completed live bars strictly after the last historical strategy timestamp."""
    if live_completed is None or live_completed.empty:
        return history.copy()
    if history is None or history.empty:
        last_time = None
    else:
        last_time = pd.to_datetime(history["strategy_time_london"].iloc[-1])

    live = live_completed.copy()
    if last_time is not None:
        live = live.loc[live["strategy_time_london"] > last_time].copy()
    if live.empty:
        return history.copy()

    rows = pd.DataFrame(
        {
            "product": instrument,
            "raw_timestamp_ms": (live["bucket_utc"].astype("int64") // 1_000_000).astype("int64"),
            "raw_time_utc": live["bucket_utc"],
            "raw_time_london": live["strategy_time_london"],
            "strategy_time_london": live["strategy_time_london"],
            "open": live["open"].astype(float),
            "high": live["high"].astype(float),
            "low": live["low"].astype(float),
            "close": live["close"].astype(float),
            "volume": 0.0,
        }
    )
    combined = pd.concat([history.copy(), rows], ignore_index=True)
    combined = combined.sort_values("strategy_time_london", kind="stable").reset_index(drop=True)
    return combined


class LiveAdminPriceHub:
    """Thread-safe wrapper around the official Lightstreamer Python client."""

    def __init__(self, max_ticks: int = 200_000) -> None:
        self._lock = threading.RLock()
        self._quotes: dict[str, dict[str, Any]] = {}
        self._ticks: deque[LiveTick] = deque(maxlen=max_ticks)
        self._status = "DISCONNECTED"
        self._error: str | None = None
        self._last_update_utc: datetime | None = None
        self._client: Any = None
        self._subscription: Any = None
        self._client_listener: Any = None
        self._subscription_listener: Any = None
        self._resolution: LiveResolution | None = None
        self._price_source = "AdminPrice"

    def _set_status(self, status: str) -> None:
        with self._lock:
            self._status = str(status)

    def _set_error(self, message: str | None) -> None:
        with self._lock:
            self._error = message

    def _on_item_update(self, update: Any) -> None:
        try:
            item_name = update.getItemName()
        except Exception:
            item_name = None
        if not item_name:
            try:
                item_name = update.getValue("key")
            except Exception:
                item_name = None
        if not item_name:
            return

        values: dict[str, Any] = {}
        for field in LIVE_FIELDS:
            try:
                value = update.getValue(field)
            except Exception:
                value = None
            if value is not None:
                values[field] = value

        received = datetime.now(timezone.utc)
        with self._lock:
            previous = self._quotes.get(str(item_name), {}).copy()
            previous.update(values)
            self._quotes[str(item_name)] = previous
            self._last_update_utc = received
            resolution = self._resolution
            source = self._price_source
            quotes_copy = {key: value.copy() for key, value in self._quotes.items()}

        if resolution is None:
            return
        snapshot = compute_structure_snapshot(
            quotes_copy,
            resolution.item_names,
            resolution.definition.coefficients,
            price_source=source,
        )
        if snapshot is None or snapshot.get("price") is None:
            return
        tick = LiveTick(
            received_at_utc=received,
            price=float(snapshot["price"]),
            bid=None if snapshot.get("bid") is None else float(snapshot["bid"]),
            ask=None if snapshot.get("ask") is None else float(snapshot["ask"]),
        )
        with self._lock:
            if self._ticks:
                last = self._ticks[-1]
                if (
                    abs((tick.received_at_utc - last.received_at_utc).total_seconds()) < 0.02
                    and tick.price == last.price
                    and tick.bid == last.bid
                    and tick.ask == last.ask
                ):
                    return
            self._ticks.append(tick)

    def start(self, resolution: LiveResolution, price_source: str = "AdminPrice") -> None:
        same = False
        with self._lock:
            if self._resolution == resolution and self._client is not None and self._price_source == price_source:
                same = True
        if same:
            return

        self.stop()
        try:
            from lightstreamer.client import (
                ClientListener,
                LightstreamerClient,
                Subscription,
                SubscriptionListener,
            )
        except ImportError as exc:
            raise LiveFeedError(
                "The Lightstreamer Python package is not installed. Run: "
                "python -m pip install lightstreamer-client-lib"
            ) from exc

        hub = self

        class _ClientListener(ClientListener):
            def onStatusChange(self, status: str) -> None:  # noqa: N802 - SDK callback name
                hub._set_status(status)

            def onServerError(self, code: int, message: str) -> None:  # noqa: N802
                hub._set_error(f"Lightstreamer server error {code}: {message}")

        class _SubscriptionListener(SubscriptionListener):
            def onItemUpdate(self, update: Any) -> None:  # noqa: N802
                hub._on_item_update(update)

            def onSubscriptionError(self, code: int, message: str) -> None:  # noqa: N802
                hub._set_error(f"Subscription error {code}: {message}")

            def onItemLostUpdates(self, item_name: str, item_pos: int, lost_updates: int) -> None:  # noqa: N802
                hub._set_error(
                    f"Live feed lost {lost_updates} update(s) for {item_name or item_pos}."
                )

        client = LightstreamerClient(LS_ENDPOINT, LS_ADAPTER_SET)
        client.connectionDetails.setUser("Test")
        client_listener = _ClientListener()
        client.addListener(client_listener)

        subscription = Subscription("MERGE", list(resolution.item_names), LIVE_FIELDS)
        subscription.setDataAdapter(LS_DATA_ADAPTER)
        subscription.setRequestedSnapshot("yes")
        subscription.setRequestedMaxFrequency("1")
        subscription_listener = _SubscriptionListener()
        subscription.addListener(subscription_listener)

        with self._lock:
            self._quotes.clear()
            self._ticks.clear()
            self._status = "CONNECTING"
            self._error = None
            self._last_update_utc = None
            self._resolution = resolution
            self._price_source = price_source
            self._client = client
            self._subscription = subscription
            self._client_listener = client_listener
            self._subscription_listener = subscription_listener

        try:
            client.subscribe(subscription)
            client.connect()
        except Exception as exc:
            with self._lock:
                self._client = None
                self._subscription = None
            raise LiveFeedError(f"Could not start Lightstreamer: {exc}") from exc

    def stop(self) -> None:
        with self._lock:
            client = self._client
            subscription = self._subscription
            self._client = None
            self._subscription = None
            self._client_listener = None
            self._subscription_listener = None
            self._resolution = None
            self._status = "DISCONNECTED"
        if client is not None:
            try:
                if subscription is not None:
                    client.unsubscribe(subscription)
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception:
                pass

    def clear_ticks(self) -> None:
        with self._lock:
            self._ticks.clear()

    def set_price_source(self, value: str) -> None:
        with self._lock:
            value = str(value)
            if value != self._price_source:
                self._price_source = value
                self._ticks.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            resolution = self._resolution
            quotes = {key: value.copy() for key, value in self._quotes.items()}
            status = self._status
            error = self._error
            last_update = self._last_update_utc
            source = self._price_source
        structure = None
        if resolution is not None:
            structure = compute_structure_snapshot(
                quotes,
                resolution.item_names,
                resolution.definition.coefficients,
                price_source=source,
            )
        return {
            "status": status,
            "error": error,
            "last_update_utc": last_update,
            "resolution": resolution,
            "quotes": quotes,
            "structure": structure,
            "price_source": source,
        }

    def ticks_frame(self) -> pd.DataFrame:
        with self._lock:
            ticks = list(self._ticks)
        if not ticks:
            return pd.DataFrame(columns=["received_at_utc", "price", "bid", "ask"])
        return pd.DataFrame(
            {
                "received_at_utc": [item.received_at_utc for item in ticks],
                "price": [item.price for item in ticks],
                "bid": [item.bid for item in ticks],
                "ask": [item.ask for item in ticks],
            }
        )
