from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


def as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class FeeSchedule:
    """Per-market fee parameters copied from gamma `feeSchedule`.

    `rebate_rate` is retained because the API sends it. Taker fees do not
    subtract it: rebates are paid to makers, not to the taker crossing the ask.
    """

    exponent: Decimal
    rate: Decimal
    taker_only: bool
    rebate_rate: Decimal
    fees_enabled: bool


@dataclass(frozen=True)
class Market:
    market_id: str
    condition_id: str
    slug: str
    question: str
    description: str
    resolution_source: str
    event_start: datetime
    end: datetime
    outcomes: tuple[str, ...]
    token_ids: dict[str, str]
    order_min_size: Decimal
    tick_size: Decimal
    fee_schedule: FeeSchedule
    accepting_orders: bool
    closed: bool
    enable_order_book: bool

    def token_id(self, side: str) -> str:
        return self.token_ids[side]


@dataclass(frozen=True)
class Ask:
    price: Decimal
    size: Decimal
    min_order_size: Decimal
    tick_size: Decimal | None = None


@dataclass(frozen=True)
class PricingInput:
    """Inputs the fair-value model is allowed to see.

    A replacement model should accept this object and return P(Up).

    `observed_seconds` are already-printed Chainlink 1-second prices,
    `(unix_second, price)`. Unix milliseconds (values at least 10**12) are
    accepted. The second just before the settlement window (`window_end - 63`)
    may be included and is used only to forward-fill a gap at the first
    second. An empty tuple means a second at or before the model clock is
    filled from the previous print, and from `current_price` when no earlier
    print is available.

    When `window_end_unix` and `as_of_unix` are both set, they are the model
    clock: `as_of_unix` is the timestamp of `current_price`. `seconds_remaining`
    is used only when `as_of_unix` is omitted.
    """

    open_price: Decimal
    current_price: Decimal
    seconds_remaining: Decimal
    volatility_per_sqrt_second: Decimal
    observed_seconds: tuple[tuple[int, Decimal], ...] = ()
    as_of_unix: int | None = None
    window_end_unix: int | None = None


@dataclass(frozen=True)
class Fill:
    market_id: str
    side: str
    price: Decimal
    size: Decimal
    fee: Decimal
    fair_prob: Decimal
    edge: Decimal
    timestamp: datetime
    mode: str
    window_start: str

    @property
    def cost(self) -> Decimal:
        return self.price * self.size

    @property
    def outlay(self) -> Decimal:
        return self.cost + self.fee
