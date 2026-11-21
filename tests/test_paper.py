from decimal import Decimal

from eth_desk.fees import taker_fee
from eth_desk.paper import capped_fill

from tests.helpers import NOW, ask, schedule


def test_paper_fill_is_capped_at_the_size_on_the_best_ask():
    fee_schedule = schedule(rate="0.13", exponent="2", rebate="0")
    level = ask("0.25", "6.5")
    fill = capped_fill(
        market_id="m1",
        side="Up",
        ask=level,
        desired_shares=Decimal("40"),
        schedule=fee_schedule,
        order_min_size=Decimal("5"),
        fair_prob=Decimal("0.90"),
        now=NOW,
        window_start="2026-11-21T05:30:00Z",
    )
    assert fill is not None
    assert fill.size == Decimal("6.5")
    assert fill.size < Decimal("40")
    assert fill.price == Decimal("0.25")
    assert fill.fee == taker_fee(Decimal("6.5"), Decimal("0.25"), fee_schedule)
    other = schedule(rate="0.01", exponent="1", rebate="0")
    assert fill.fee != taker_fee(Decimal("6.5"), Decimal("0.25"), other)


def test_paper_fill_respects_order_min_size_when_the_book_is_short():
    fill = capped_fill(
        market_id="m1",
        side="Down",
        ask=ask("0.30", "4"),
        desired_shares=Decimal("10"),
        schedule=schedule(rate="0.01"),
        order_min_size=Decimal("5"),
        fair_prob=Decimal("0.90"),
        now=NOW,
        window_start="2026-11-21T05:30:00Z",
    )
    assert fill is None
