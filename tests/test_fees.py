import json
from decimal import Decimal
from pathlib import Path

from eth_desk.fees import taker_fee
from eth_desk.gamma import parse_market

from tests.helpers import schedule

FIXTURES = Path(__file__).parent / "fixtures"


def test_exponent_one_matches_the_published_formula():
    fee_schedule = schedule(rate="0.01", exponent="1", rebate="0")
    assert taker_fee(Decimal("100"), Decimal("0.50"), fee_schedule) == Decimal("0.25")
    assert taker_fee(Decimal("100"), Decimal("0.10"), fee_schedule) == Decimal("0.09")
    assert taker_fee(Decimal("100"), Decimal("0.01"), fee_schedule) == Decimal("0.00990")


def test_fee_uses_the_supplied_schedule_instead_of_a_constant():
    price = Decimal("0.40")
    shares = Decimal("10")
    low = taker_fee(shares, price, schedule(rate="0.01", rebate="0"))
    high = taker_fee(shares, price, schedule(rate="0.20", rebate="0"))
    curved = taker_fee(shares, price, schedule(rate="0.20", exponent="2", rebate="0"))
    assert low == Decimal("0.024")
    assert high == Decimal("0.48")
    assert curved == Decimal("0.1152")
    assert len({low, high, curved}) == 3


def test_maker_rebate_rate_is_not_subtracted_from_the_taker():
    left = taker_fee(Decimal("10"), Decimal("0.40"), schedule(rate="0.20", rebate="0"))
    right = taker_fee(Decimal("10"), Decimal("0.40"), schedule(rate="0.20", rebate="0.2"))
    assert left == right


def test_disabled_fees_and_dust_are_zero():
    assert taker_fee(Decimal("10"), Decimal("0.40"), schedule(enabled=False, rate="0.5")) == Decimal("0")
    # 1 * 0.001 * 0.01 * 0.99 = 0.0000099, below the 0.00001 minimum.
    dust = taker_fee(Decimal("1"), Decimal("0.01"), schedule(rate="0.001", rebate="0"))
    assert dust == Decimal("0")


def test_recorded_gamma_schedule_is_not_the_legacy_base_fee():
    events = json.loads((FIXTURES / "gamma_events.json").read_text())
    market = parse_market(events[0]["markets"][0])
    assert market is not None
    assert market.fee_schedule.rate == Decimal(str(events[0]["markets"][0]["feeSchedule"]["rate"]))
    assert market.fee_schedule.exponent == Decimal("1")
    fee = taker_fee(Decimal("100"), Decimal("0.50"), market.fee_schedule)
    assert fee == Decimal("0.25")
    # takerBaseFee on this payload is 1000. Treating that as 10% of a $50 trade would be $5.
    assert events[0]["markets"][0]["takerBaseFee"] == 1000
    assert fee != Decimal("5")
