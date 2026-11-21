from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from eth_desk.fees import taker_fee
from eth_desk.limits import (
    MAX_BET_USD,
    MAX_BUY_PRICE,
    MIN_EDGE,
    SHARE_QUANTUM,
)
from eth_desk.types import Ask, FeeSchedule, Fill


def capped_fill(
    *,
    market_id: str,
    side: str,
    ask: Ask,
    desired_shares: Decimal,
    schedule: FeeSchedule,
    order_min_size: Decimal,
    fair_prob: Decimal,
    now: datetime,
    window_start: str,
    mode: str = "paper",
) -> Fill | None:
    """Buy at the best ask, never for more shares than that level has.

    The fee is computed from `schedule` for the capped size. Returns None when
    the capped size is below `order_min_size` or no longer clears the edge rule.
    """

    if ask.size <= 0 or ask.price <= 0:
        return None
    if ask.price > MAX_BUY_PRICE:
        return None
    shares = min(desired_shares, ask.size).quantize(SHARE_QUANTUM, rounding=ROUND_DOWN)
    if shares <= 0 or shares > ask.size or shares < order_min_size:
        return None
    fee = taker_fee(shares, ask.price, schedule)
    outlay = shares * ask.price + fee
    if outlay > MAX_BET_USD:
        return None
    edge = fair_prob - ask.price - (fee / shares)
    if edge < MIN_EDGE:
        return None
    return Fill(
        market_id=market_id,
        side=side,
        price=ask.price,
        size=shares,
        fee=fee,
        fair_prob=fair_prob,
        edge=edge,
        timestamp=now,
        mode=mode,
        window_start=window_start,
    )
