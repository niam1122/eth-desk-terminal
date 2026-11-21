from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlparse

from eth_desk.clock import iso_z, seconds_between
from eth_desk.types import Market, PricingInput

RTDS_URL = "wss://ws-live-data.polymarket.com"
CRYPTO_PRICE_URL = "https://polymarket.com/api/crypto/crypto-price"
_E18 = Decimal(10) ** 18


class UnrecognizedSettlementSource(ValueError):
    """The market's resolutionSource is not the Chainlink ETH/USD stream we can read."""


@dataclass(frozen=True)
class StreamSpec:
    topic: str
    symbol: str
    asset: str
    variant: str


@dataclass(frozen=True)
class WindowPrint:
    open_price: Decimal | None
    close_price: Decimal | None
    completed: bool


def stream_spec(resolution_source: str, window_seconds: Decimal) -> StreamSpec:
    """Map the market's resolutionSource URL onto the public Chainlink stream."""

    parsed = urlparse(resolution_source)
    host = parsed.netloc.lower()
    if host not in {"data.chain.link", "www.data.chain.link"}:
        raise UnrecognizedSettlementSource(resolution_source)
    slug = parsed.path.rstrip("/").split("/")[-1].lower()
    if not slug.startswith("eth-usd"):
        raise UnrecognizedSettlementSource(resolution_source)
    if "twap-60s" in slug:
        topic = "crypto_prices_twap_sixty"
    elif "twap" not in slug:
        topic = "crypto_prices_chainlink"
    else:
        raise UnrecognizedSettlementSource(resolution_source)
    if not (Decimal("840") <= window_seconds <= Decimal("960")):
        raise UnrecognizedSettlementSource(
            f"window of {window_seconds}s is not the 15-minute ETH market"
        )
    return StreamSpec(topic=topic, symbol="eth/usd", asset="ETH", variant="fifteen")


def crypto_price_params(spec: StreamSpec, event_start: datetime, end: datetime) -> dict[str, str]:
    """Query for the window's Chainlink open (and close, once the window is over).

    `variant=fifteen` is required. Other values are a different series.
    """

    return {
        "symbol": spec.asset,
        "eventStartTime": iso_z(event_start),
        "variant": spec.variant,
        "endDate": iso_z(end),
    }


def parse_window_print(body: object) -> WindowPrint:
    if not isinstance(body, dict):
        raise UnrecognizedSettlementSource("crypto price payload was not an object")

    def number(key: str) -> Decimal | None:
        value = body.get(key)
        if value is None:
            return None
        return Decimal(str(value))

    return WindowPrint(
        open_price=number("openPrice"),
        close_price=number("closePrice"),
        completed=bool(body.get("completed")),
    )


def up_wins(open_price: Decimal, close_price: Decimal) -> bool:
    """Market rule: Up when the settlement print is >= the window's open print."""

    return close_price >= open_price


def parse_price_point(row: dict) -> tuple[int, Decimal]:
    """One Chainlink print.

    Legacy RTDS `full_accuracy_value` is an 18-decimal integer. A value that
    already contains a decimal point is used as-is.
    """

    timestamp = int(row["timestamp"])
    full = row.get("full_accuracy_value")
    if full is not None:
        text = str(full).strip()
        if text.startswith(("0x", "0X")):
            return timestamp, Decimal(int(text, 16)) / _E18
        digits = text.lstrip("-")
        if "." not in text and "e" not in text.lower() and digits.isdigit() and len(digits) > 12:
            return timestamp, Decimal(text) / _E18
        return timestamp, Decimal(text)
    return timestamp, Decimal(str(row["value"]))


def points_from_message(message: dict, spec: StreamSpec) -> list[tuple[int, Decimal]]:
    topic = message.get("topic")
    if topic is not None and topic != spec.topic:
        return []
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    symbol = payload.get("symbol")
    if symbol is not None and symbol != spec.symbol:
        return []
    rows: list[dict] = []
    data = payload.get("data")
    if isinstance(data, list):
        rows.extend(item for item in data if isinstance(item, dict))
    if "value" in payload or "full_accuracy_value" in payload:
        rows.append(payload)
    points = []
    for row in rows:
        row_symbol = row.get("symbol")
        if row_symbol is not None and row_symbol != spec.symbol:
            continue
        if "timestamp" not in row:
            continue
        if "value" not in row and "full_accuracy_value" not in row:
            continue
        points.append(parse_price_point(row))
    return points


def volatility_per_sqrt_second(points: Sequence[tuple[int, Decimal]]) -> Decimal | None:
    """RMS of log returns scaled to a one-second variance. Needs 8 intervals."""

    ordered = sorted(points, key=lambda item: item[0])
    samples: list[Decimal] = []
    for (t0, p0), (t1, p1) in zip(ordered, ordered[1:]):
        dt_ms = t1 - t0
        if dt_ms <= 0 or p0 <= 0 or p1 <= 0:
            continue
        dt = Decimal(dt_ms) / Decimal(1000)
        samples.append((p1 / p0).ln() / dt.sqrt())
    if len(samples) < 8:
        return None
    mean_square = sum((sample * sample for sample in samples), Decimal("0")) / Decimal(len(samples))
    if mean_square <= 0:
        return Decimal("0")
    return mean_square.sqrt()


def latest_fresh(
    points: Sequence[tuple[int, Decimal]],
    now_ms: int,
    max_age_ms: int = 15_000,
) -> Decimal | None:
    if not points:
        return None
    timestamp, price = max(points, key=lambda item: item[0])
    if now_ms - timestamp > max_age_ms:
        return None
    if price <= 0:
        return None
    return price


def read_rtds_points(spec: StreamSpec, *, url: str = RTDS_URL, wait_seconds: float = 3.0):
    """Subscribe to one Chainlink topic and collect the snapshot plus live ticks."""

    import asyncio

    import websockets

    async def _read() -> list[tuple[int, Decimal]]:
        points: list[tuple[int, Decimal]] = []
        async with websockets.connect(
            url,
            additional_headers={
                "User-Agent": "eth-desk/0.1",
                "Origin": "https://polymarket.com",
            },
            open_timeout=10,
            close_timeout=2,
        ) as ws:
            subscription = {
                "action": "subscribe",
                "subscriptions": [
                    {
                        "topic": spec.topic,
                        "type": "*",
                        "filters": json.dumps({"symbol": spec.symbol}),
                    }
                ],
            }
            await ws.send(json.dumps(subscription))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_seconds
            while True:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    points.extend(points_from_message(message, spec))
        return points

    return asyncio.run(_read())


class _WindowView:
    def __init__(self, resolution_source: str, event_start: datetime, end: datetime):
        self.resolution_source = resolution_source
        self.event_start = event_start
        self.end = end


class ChainlinkFeed:
    """Open print from the crypto-price series; live prints from the named stream."""

    def __init__(self, transport=None, reader=read_rtds_points, price_url: str = CRYPTO_PRICE_URL):
        from eth_desk.http import HttpxJson

        self.transport = transport or HttpxJson()
        self.reader = reader
        self.price_url = price_url

    def pricing_input(self, market: Market, now: datetime) -> PricingInput | None:
        window = self._fetch(market.resolution_source, market.event_start, market.end)
        if window is None or window.open_price is None or window.open_price <= 0:
            return None
        spec = stream_spec(
            market.resolution_source,
            seconds_between(market.event_start, market.end),
        )
        points = self.reader(spec)
        now_ms = int(now.timestamp() * 1000)
        current = latest_fresh(points, now_ms)
        if current is None:
            return None
        volatility = volatility_per_sqrt_second(points)
        if volatility is None or volatility <= 0:
            return None
        return PricingInput(
            open_price=window.open_price,
            current_price=current,
            seconds_remaining=seconds_between(now, market.end),
            volatility_per_sqrt_second=volatility,
        )

    def window_close(self, position) -> WindowPrint | None:
        try:
            return self._fetch(position.resolution_source, position.event_start, position.window_end)
        except Exception:
            return None

    def _fetch(self, resolution_source: str, event_start: datetime, end: datetime) -> WindowPrint | None:
        spec = stream_spec(resolution_source, seconds_between(event_start, end))
        params = crypto_price_params(spec, event_start, end)
        body = self.transport(self.price_url, params)
        return parse_window_print(body)
