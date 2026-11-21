from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from eth_desk.limits import BANKROLL_USD
from eth_desk.types import Ask, FeeSchedule, Fill, Market

NOW = datetime(2026, 11, 21, 5, 42, tzinfo=timezone.utc)
START = datetime(2026, 11, 21, 5, 30, tzinfo=timezone.utc)
END = datetime(2026, 11, 21, 5, 45, tzinfo=timezone.utc)
RESOLUTION = "https://data.chain.link/streams/eth-usd-twap-60s-streams"


def schedule(
    rate="0.01",
    exponent="1",
    enabled=True,
    rebate="0.2",
) -> FeeSchedule:
    return FeeSchedule(
        exponent=Decimal(exponent),
        rate=Decimal(rate),
        taker_only=True,
        rebate_rate=Decimal(rebate),
        fees_enabled=enabled,
    )


def make_market(fee_schedule: FeeSchedule | None = None, order_min_size: str = "5") -> Market:
    return Market(
        market_id="7191736",
        condition_id="0xabc",
        slug="eth-updown-15m-1795239000",
        question="Ethereum Up or Down",
        description="Resolves from the Chainlink ETH/USD TWAP stream.",
        resolution_source=RESOLUTION,
        event_start=START,
        end=END,
        outcomes=("Up", "Down"),
        token_ids={"Up": "up-token", "Down": "down-token"},
        order_min_size=Decimal(order_min_size),
        tick_size=Decimal("0.01"),
        fee_schedule=fee_schedule or schedule(),
        accepting_orders=True,
        closed=False,
        enable_order_book=True,
    )


def passing(**overrides):
    base = dict(
        price=Decimal("0.40"),
        book_size=Decimal("100"),
        fair_prob=Decimal("0.90"),
        fee_schedule=schedule(),
        order_min_size=Decimal("5"),
        seconds_remaining=Decimal("180"),
        daily_pnl=Decimal("0"),
        daily_stop_latched=False,
        window_spent=Decimal("0"),
        cash=BANKROLL_USD,
    )
    base.update(overrides)
    return base


def make_fill(**overrides) -> Fill:
    base = dict(
        market_id="7191736",
        side="Up",
        price=Decimal("0.50"),
        size=Decimal("10"),
        fee=Decimal("0"),
        fair_prob=Decimal("0.80"),
        edge=Decimal("0.30"),
        timestamp=NOW,
        mode="paper",
        window_start="2026-11-21T05:30:00Z",
    )
    base.update(overrides)
    return Fill(**base)


def ask(price, size, min_size="5") -> Ask:
    return Ask(Decimal(price), Decimal(size), Decimal(min_size))


def later(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)
