from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlparse

from eth_desk.clock import iso_z, seconds_between, utcnow
from eth_desk.pricing import observed_settlement_seconds
from eth_desk.types import Market, PricingInput

RTDS_URL = "wss://ws-live-data.polymarket.com"
CRYPTO_PRICE_URL = "https://polymarket.com/api/crypto/crypto-price"
CHAINLINK_1S_TOPIC = "crypto_prices_chainlink"
CHAINLINK_1S_SYMBOL = "eth/usd"
# The subscribe backfill arrives on this topic, not on crypto_prices_chainlink.
# Confirmed live: type "subscribe", payload.symbol "eth/usd", payload.data is
# the recent one-second history. Live ticks then use crypto_prices_chainlink.
CHAINLINK_HISTORY_TOPIC = "crypto_prices"
RTDS_PING_SECONDS = 5.0
RTDS_RECONNECT_SECONDS = 1.0
_log = logging.getLogger("eth_desk.prices")
# Plain average of this many 1-second Chainlink prints. Not the blended
# weights inside TwapSettlementModel; those stay on the probability model.
TWAP_SECONDS = 60
DEFAULT_TWAP_OFFSET_SECONDS = 2
DEFAULT_MIN_TWAP_PRINTS = TWAP_SECONDS
_BUFFER_MAX_AGE_SECONDS = 6 * 60 * 60
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


def accepted_topics(spec: StreamSpec) -> set[str]:
    """Topics that carry this spec's prints.

    The chainlink subscription's first payload is a history snapshot labeled
    `crypto_prices`. Later ticks are `crypto_prices_chainlink`.
    """

    topics = {spec.topic}
    if spec.topic == CHAINLINK_1S_TOPIC:
        topics.add(CHAINLINK_HISTORY_TOPIC)
    return topics


def chainlink_subscribe_message(spec: StreamSpec) -> dict:
    """RTDS subscribe frame. `filters` is a JSON string, not an object."""

    return {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": spec.topic,
                "type": "*",
                "filters": json.dumps({"symbol": spec.symbol}, separators=(",", ":")),
            }
        ],
    }


def points_from_frame(raw: str | bytes, spec: StreamSpec) -> list[tuple[int, Decimal]]:
    """Prints from one websocket frame. Non-JSON frames (PING replies, empties) are ignored."""

    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = raw
    if not text or not text.lstrip().startswith("{"):
        return []
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(message, dict):
        return []
    return points_from_message(message, spec)


def points_from_message(message: dict, spec: StreamSpec) -> list[tuple[int, Decimal]]:
    topic = message.get("topic")
    if topic is not None and topic not in accepted_topics(spec):
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


def _unix_seconds(timestamp: int) -> int:
    value = int(timestamp)
    if value >= 1_000_000_000_000:
        return value // 1000
    return value


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


def _short_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:200]


class RtdsChainlinkSession:
    """One background thread and one RTDS websocket for the 1-second Chainlink feed.

    The socket stays open, sends the text frame `PING` every 5 seconds, and on
    any error waits 1 second, reconnects, and subscribes again. The server's
    first message after subscribe refills about the last minute.
    """

    def __init__(self, spec: StreamSpec, buffer: ChainlinkPrintBuffer, url: str = RTDS_URL):
        self.spec = spec
        self.buffer = buffer
        self.url = url
        self.connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._events: list[dict] = []
        self._events_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rtds-chainlink", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=5)

    def drain_status(self) -> list[dict]:
        with self._events_lock:
            events = self._events
            self._events = []
        return events

    def _emit(self, event: str, **fields: object) -> None:
        record = {
            "type": "rtds",
            "event": event,
            "topic": self.spec.topic,
            "symbol": self.spec.symbol,
            "timestamp": iso_z(utcnow()),
        }
        record.update(fields)
        with self._events_lock:
            self._events.append(record)
        detail = fields.get("error") or ""
        _log.info("rtds %s %s %s", event, self.spec.topic, detail)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as exc:
                self.connected = False
                self._emit("disconnect", error=_short_error(exc))
            else:
                self.connected = False
                if self._stop.is_set():
                    self._emit("disconnect")
                    return
                self._emit("disconnect", error="closed")
            if self._stop.is_set():
                return
            self._emit("reconnect", wait_seconds=RTDS_RECONNECT_SECONDS)
            if self._stop.wait(RTDS_RECONNECT_SECONDS):
                return

    def _session(self) -> None:
        import asyncio

        asyncio.run(self._async_session())

    async def _async_session(self) -> None:
        import asyncio

        import websockets

        async with websockets.connect(
            self.url,
            additional_headers={
                "User-Agent": "eth-desk/0.1",
                "Origin": "https://polymarket.com",
            },
            open_timeout=20,
            close_timeout=2,
            max_size=8_000_000,
        ) as ws:
            self.connected = True
            self._emit("connect")
            await ws.send(json.dumps(chainlink_subscribe_message(self.spec)))
            ping = asyncio.create_task(self._ping(ws))
            try:
                while not self._stop.is_set():
                    if ping.done():
                        error = ping.exception()
                        if error is not None:
                            raise error
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    points = points_from_frame(raw, self.spec)
                    if points:
                        self.buffer.extend(points)
            finally:
                ping.cancel()
                try:
                    await ping
                except asyncio.CancelledError:
                    pass
                self.connected = False

    async def _ping(self, ws) -> None:
        import asyncio

        while True:
            await asyncio.sleep(RTDS_PING_SECONDS)
            await ws.send("PING")


@dataclass(frozen=True)
class BoundaryTwap:
    """Plain average of the 1-second prints in one settlement minute.

    `price` is None when fewer than `minimum` seconds in `[start, end]` have
    a print. Gaps are not filled.
    """

    price: Decimal | None
    count: int
    start: int
    end: int
    offset: int
    minimum: int
    window_seconds: int

    @property
    def ready(self) -> bool:
        return self.price is not None


def boundary_window(
    boundary_unix: int,
    *,
    seconds: int = TWAP_SECONDS,
    offset: int = DEFAULT_TWAP_OFFSET_SECONDS,
) -> tuple[int, int]:
    """Inclusive unix seconds of the plain settlement average.

    The window is the `seconds` one-second prints ending at
    `boundary - offset`. Offset 2 is `[boundary - 61, boundary - 2]`.
    Offset 3 is the other published alignment, `[boundary - 62, boundary - 3]`.
    """

    if seconds < 1:
        raise ValueError("twap window must cover at least one second")
    if offset < 0:
        raise ValueError("twap offset must be >= 0")
    end = _unix_seconds(boundary_unix) - int(offset)
    start = end - (int(seconds) - 1)
    return start, end


def boundary_twap(
    prices: Mapping[int, Decimal],
    boundary_unix: int,
    *,
    seconds: int = TWAP_SECONDS,
    offset: int = DEFAULT_TWAP_OFFSET_SECONDS,
    min_prints: int = DEFAULT_MIN_TWAP_PRINTS,
) -> BoundaryTwap:
    """Unweighted mean of the 1-second Chainlink prints in the settlement minute.

    `prices` is keyed by unix second. A missing second is a gap: it is not
    forward-filled and it is not treated as zero. When fewer than `min_prints`
    seconds have a price, `price` is None and the caller must not trade.
    """

    start, end = boundary_window(boundary_unix, seconds=seconds, offset=offset)
    chosen = [prices[second] for second in range(start, end + 1) if second in prices]
    count = len(chosen)
    if count < min_prints or count == 0:
        return BoundaryTwap(None, count, start, end, int(offset), int(min_prints), int(seconds))
    mean = sum(chosen, Decimal("0")) / Decimal(count)
    return BoundaryTwap(mean, count, start, end, int(offset), int(min_prints), int(seconds))


def twap_gap_reason(kind: str, quote: BoundaryTwap, slug: str = "") -> str:
    """Logged when the settlement minute does not have enough 1-second prints."""

    text = (
        f"twap_gap {kind}: {quote.count} of {quote.window_seconds} Chainlink 1s prints "
        f"in [{quote.start}, {quote.end}] (offset {quote.offset}s, minimum {quote.minimum})"
    )
    if slug:
        text = f"{text} slug={slug}"
    return text


def _as_unix_ms(timestamp: int) -> int:
    value = int(timestamp)
    if value >= 1_000_000_000_000 or value < 0:
        return value
    return value * 1000


@dataclass
class _SecondPrint:
    timestamp_ms: int
    price: Decimal


class ChainlinkPrintBuffer:
    """Rolling 1-second Chainlink prints, one price per unix second.

    Millisecond timestamps are stored under `timestamp // 1000`. The first
    print that arrives for a second is kept; a later tick in that same second
    does not replace it.
    """

    def __init__(self, max_age_seconds: int = _BUFFER_MAX_AGE_SECONDS):
        self.max_age_seconds = max_age_seconds
        self._by_second: dict[int, _SecondPrint] = {}
        self._lock = threading.Lock()

    def extend(self, points: Sequence[tuple[int, Decimal]]) -> None:
        with self._lock:
            for timestamp, price in points:
                if price is None or price <= 0:
                    continue
                timestamp_ms = _as_unix_ms(int(timestamp))
                second = _unix_seconds(timestamp_ms)
                if second not in self._by_second:
                    self._by_second[second] = _SecondPrint(timestamp_ms, price)

    def prune(self, now_unix: int) -> None:
        cutoff = int(now_unix) - self.max_age_seconds
        with self._lock:
            if not self._by_second:
                return
            self._by_second = {
                second: item for second, item in self._by_second.items() if second >= cutoff
            }

    def prices(self) -> dict[int, Decimal]:
        with self._lock:
            return {second: item.price for second, item in self._by_second.items()}

    def points(self) -> list[tuple[int, Decimal]]:
        with self._lock:
            return [(item.timestamp_ms, item.price) for item in self._by_second.values()]


class ChainlinkFeed:
    """Window open and close from the 1-second Chainlink settlement prints.

    The buffer is filled by one persistent RTDS connection to
    `crypto_prices_chainlink` (symbol `eth/usd`): the subscribe backfill plus
    each live tick. The open, and the close used for paper settlement, are the
    plain average of the 60 prints ending `twap_offset_seconds` before the
    boundary (default 2). The crypto-price endpoint is not consulted.
    `last_skip_reason` is set when the minute does not have enough prints.

    Pass `reader` only to inject prints in tests. The default opens the live socket.
    """

    def __init__(
        self,
        transport=None,
        reader=None,
        price_url: str = CRYPTO_PRICE_URL,
        *,
        twap_offset_seconds: int = DEFAULT_TWAP_OFFSET_SECONDS,
        min_twap_prints: int = DEFAULT_MIN_TWAP_PRINTS,
        buffer: ChainlinkPrintBuffer | None = None,
    ):
        from eth_desk.http import HttpxJson

        if twap_offset_seconds < 0:
            raise ValueError("twap offset must be >= 0")
        if min_twap_prints < 1 or min_twap_prints > TWAP_SECONDS:
            raise ValueError(f"min twap prints must be from 1 to {TWAP_SECONDS}")
        # Kept so existing callers can still pass a transport. Boundary prices
        # are computed from the 1-second buffer, not from this URL.
        self.transport = transport or HttpxJson()
        self.reader = reader
        self.price_url = price_url
        self.twap_offset_seconds = int(twap_offset_seconds)
        self.min_twap_prints = int(min_twap_prints)
        self.buffer = buffer or ChainlinkPrintBuffer()
        self.chainlink_spec = StreamSpec(
            topic=CHAINLINK_1S_TOPIC,
            symbol=CHAINLINK_1S_SYMBOL,
            asset="ETH",
            variant="fifteen",
        )
        self.last_skip_reason: str | None = None
        self.last_refresh_error: str | None = None
        self._session = (
            RtdsChainlinkSession(self.chainlink_spec, self.buffer) if reader is None else None
        )

    def start(self) -> None:
        if self._session is not None:
            self._session.start()

    def stop(self) -> None:
        if self._session is not None:
            self._session.stop()

    def drain_status(self) -> list[dict]:
        if self._session is None:
            return []
        return self._session.drain_status()

    def pricing_input(self, market: Market, now: datetime) -> PricingInput | None:
        self.last_skip_reason = None
        stream_spec(
            market.resolution_source,
            seconds_between(market.event_start, market.end),
        )
        self._ingest(now)
        opened = self._quote(market.event_start)
        if not opened.ready:
            self.last_skip_reason = self._gap("open", opened, market.slug)
            return None
        points = self.buffer.points()
        now_ms = int(now.timestamp() * 1000)
        current = latest_fresh(points, now_ms)
        if current is None:
            self.last_skip_reason = f"stale_or_missing Chainlink 1s price slug={market.slug}"
            return None
        volatility = volatility_per_sqrt_second(points)
        if volatility is None or volatility <= 0:
            self.last_skip_reason = f"chainlink volatility unavailable slug={market.slug}"
            return None
        timestamp, _price = max(points, key=lambda item: item[0])
        window_end_unix = int(market.end.timestamp())
        return PricingInput(
            open_price=opened.price,
            current_price=current,
            seconds_remaining=seconds_between(now, market.end),
            volatility_per_sqrt_second=volatility,
            observed_seconds=observed_settlement_seconds(points, window_end_unix),
            as_of_unix=_unix_seconds(timestamp),
            window_end_unix=window_end_unix,
        )

    def window_close(self, position, now: datetime | None = None) -> WindowPrint | None:
        """TWAP close and TWAP open for a paper settlement. None when either minute is short."""

        self.last_skip_reason = None
        when = now if now is not None else utcnow()
        slug = getattr(position, "slug", "") or ""
        try:
            stream_spec(
                position.resolution_source,
                seconds_between(position.event_start, position.window_end),
            )
        except UnrecognizedSettlementSource as exc:
            self.last_skip_reason = f"unrecognized settlement source slug={slug}: {exc}"
            return None
        self._ingest(when)
        opened = self._quote(position.event_start)
        closed = self._quote(position.window_end)
        reasons = []
        if not opened.ready:
            reasons.append(self._gap("open", opened, slug))
        if not closed.ready:
            reasons.append(self._gap("close", closed, slug))
        if reasons:
            self.last_skip_reason = "; ".join(reasons)
            return None
        return WindowPrint(open_price=opened.price, close_price=closed.price, completed=True)

    def _quote(self, boundary: datetime) -> BoundaryTwap:
        return boundary_twap(
            self.buffer.prices(),
            int(boundary.timestamp()),
            seconds=TWAP_SECONDS,
            offset=self.twap_offset_seconds,
            min_prints=self.min_twap_prints,
        )

    def _gap(self, kind: str, quote: BoundaryTwap, slug: str) -> str:
        reason = twap_gap_reason(kind, quote, slug)
        if self.last_refresh_error:
            reason = f"{reason}; chainlink refresh failed ({self.last_refresh_error})"
        return reason

    def _ingest(self, now: datetime) -> None:
        if self._session is not None:
            self._session.start()
            self.last_refresh_error = None
            self.buffer.prune(int(now.timestamp()))
            return
        try:
            points = self.reader(self.chainlink_spec) or []
        except Exception as exc:
            self.last_refresh_error = type(exc).__name__
            return
        self.last_refresh_error = None
        self.buffer.extend(points)
        self.buffer.prune(int(now.timestamp()))
