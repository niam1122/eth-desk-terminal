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
    """

    open_price: Decimal
    current_price: Decimal
    seconds_remaining: Decimal
    volatility_per_sqrt_second: Decimal


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
