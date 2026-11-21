from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from eth_desk.fees import taker_fee
from eth_desk.types import FeeSchedule

# Hard limits. These are not configuration. Environment variables cannot raise them.
BANKROLL_USD = Decimal("250")
MAX_BET_USD = Decimal("5")
MAX_WINDOW_USD = Decimal("25")
MAX_BUY_PRICE = Decimal("0.72")
LAST_TRADE_SECONDS = Decimal("240")
DAILY_STOP_LOSS_USD = Decimal("15")
MIN_EDGE = Decimal("0.06")
SHARE_QUANTUM = Decimal("0.01")

REASON_OK = "ok"
REASON_DAILY_STOP = "daily_stop"
REASON_WINDOW_TIME = "outside_last_4_minutes"
REASON_PRICE = "price_above_72c"
REASON_NO_LIQUIDITY = "no_liquidity"
REASON_MIN_SIZE = "below_order_min_size"
REASON_MAX_BET = "max_bet"
REASON_WINDOW_CAP = "window_cap"
REASON_BANKROLL = "bankroll"
REASON_EDGE = "edge_below_6_points_after_fee"


class LimitBreach(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TradeDecision:
    allowed: bool
    reason: str
    shares: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    edge: Decimal = Decimal("0")
    outlay: Decimal = Decimal("0")
    fair_prob: Decimal = Decimal("0")


def _reject(reason: str, *, price: Decimal = Decimal("0"), fair: Decimal = Decimal("0")) -> TradeDecision:
    return TradeDecision(False, reason, price=price, fair_prob=fair)


def cash_outlay(shares: Decimal, price: Decimal, schedule: FeeSchedule) -> Decimal:
    return shares * price + taker_fee(shares, price, schedule)


def shares_for_budget(
    price: Decimal,
    schedule: FeeSchedule,
    budget: Decimal,
    cap_shares: Decimal,
) -> Decimal:
    """Largest 0.01-share lot whose price + fee fits in `budget` and `cap_shares`."""

    if price <= 0 or budget <= 0 or cap_shares <= 0:
        return Decimal("0")
    shares = min(
        (budget / price).quantize(SHARE_QUANTUM, rounding=ROUND_DOWN),
        cap_shares.quantize(SHARE_QUANTUM, rounding=ROUND_DOWN),
    )
    steps = 0
    while shares > 0 and cash_outlay(shares, price, schedule) > budget:
        shares -= SHARE_QUANTUM
        steps += 1
        if steps > 100_000:
            return Decimal("0")
    if shares < 0:
        return Decimal("0")
    return shares


def decide_bet(
    *,
    price: Decimal,
    book_size: Decimal,
    fair_prob: Decimal,
    fee_schedule: FeeSchedule,
    order_min_size: Decimal,
    seconds_remaining: Decimal,
    daily_pnl: Decimal,
    daily_stop_latched: bool,
    window_spent: Decimal,
    cash: Decimal,
) -> TradeDecision:
    """Apply every hard limit. Returns the sized buy, or a rejection reason.

    Dollar limits are on cash outlay (share cost plus the taker fee).
    """

    if daily_stop_latched or daily_pnl <= -DAILY_STOP_LOSS_USD:
        return _reject(REASON_DAILY_STOP, price=price, fair=fair_prob)
    if seconds_remaining <= 0 or seconds_remaining > LAST_TRADE_SECONDS:
        return _reject(REASON_WINDOW_TIME, price=price, fair=fair_prob)
    if price > MAX_BUY_PRICE:
        return _reject(REASON_PRICE, price=price, fair=fair_prob)
    if price <= 0 or book_size <= 0:
        return _reject(REASON_NO_LIQUIDITY, price=price, fair=fair_prob)

    remaining_window = MAX_WINDOW_USD - window_spent
    if remaining_window <= 0:
        return _reject(REASON_WINDOW_CAP, price=price, fair=fair_prob)
    if cash <= 0:
        return _reject(REASON_BANKROLL, price=price, fair=fair_prob)

    budget = min(MAX_BET_USD, remaining_window, cash)
    shares = shares_for_budget(price, fee_schedule, budget, book_size)
    if shares < order_min_size:
        minimum = cash_outlay(order_min_size, price, fee_schedule)
        if minimum > MAX_BET_USD:
            return _reject(REASON_MAX_BET, price=price, fair=fair_prob)
        if window_spent + minimum > MAX_WINDOW_USD:
            return _reject(REASON_WINDOW_CAP, price=price, fair=fair_prob)
        if minimum > cash:
            return _reject(REASON_BANKROLL, price=price, fair=fair_prob)
        return _reject(REASON_MIN_SIZE, price=price, fair=fair_prob)

    fee = taker_fee(shares, price, fee_schedule)
    outlay = shares * price + fee
    if outlay > MAX_BET_USD:
        return _reject(REASON_MAX_BET, price=price, fair=fair_prob)
    if window_spent + outlay > MAX_WINDOW_USD:
        return _reject(REASON_WINDOW_CAP, price=price, fair=fair_prob)
    if outlay > cash:
        return _reject(REASON_BANKROLL, price=price, fair=fair_prob)

    edge = fair_prob - price - (fee / shares)
    if edge < MIN_EDGE:
        return _reject(REASON_EDGE, price=price, fair=fair_prob)
    return TradeDecision(
        True,
        REASON_OK,
        shares=shares,
        price=price,
        fee=fee,
        edge=edge,
        outlay=outlay,
        fair_prob=fair_prob,
    )
