"""TWAP settlement model: closed form, final minute, ties, and gaps."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from eth_desk.cli import default_services
from eth_desk.prices import ChainlinkFeed
from eth_desk.pricing import (
    TwapSettlementModel,
    ZeroDriftLognormalModel,
    ewma_sigma_per_sqrt_second,
    far_from_expiry_approximation,
    observed_settlement_seconds,
    settlement_twap,
)
from eth_desk.types import PricingInput

from tests.helpers import END, START, make_market

END_UNIX = 1_700_000_000
SIGMA = Decimal("0.0001")


def _input(spot, open_price="2000", seconds="600", sigma=SIGMA, **kwargs) -> PricingInput:
    return PricingInput(
        Decimal(open_price),
        Decimal(spot),
        Decimal(seconds),
        Decimal(sigma) if not isinstance(sigma, Decimal) else sigma,
        **kwargs,
    )


def test_far_from_expiry_matches_the_closed_form():
    model = TwapSettlementModel()
    assert model.name == "twap_settlement"
    # Nothing in the settlement window has printed yet (that window is the last ~62s).
    for spot in ("1990", "2000", "2010", "2020"):
        for seconds in ("240", "600"):
            priced = model.probability_up(_input(spot, seconds=seconds))
            approx = far_from_expiry_approximation(float(spot), 2000.0, float(seconds), 1e-4)
            assert abs(float(priced) - approx) < 0.001
    at_the_money = model.probability_up(_input("2000"))
    assert at_the_money == Decimal("0.50000000")
    # Locked against an independent evaluation of Φ((S−O) / (S σ √V)), V = 557.9986111111.
    assert model.probability_up(_input("2010")) == Decimal("0.98240393")


def test_final_minute_uses_observed_settlement_seconds():
    model = TwapSettlementModel()
    now = END_UNIX - 30
    observed = tuple(
        (second, Decimal("100.02"))
        for second in range(END_UNIX - 62, END_UNIX - 2 + 1)
        if second <= now
    )
    kwargs = dict(as_of_unix=now, window_end_unix=END_UNIX, seconds="30")
    partial = model.probability_up(_input("100", open_price="100", observed_seconds=observed, **kwargs))
    flat = model.probability_up(_input("100", open_price="100", observed_seconds=(), **kwargs))
    assert partial == Decimal("0.77636884")
    assert flat == Decimal("0.50000000")
    # A print that is still in the future does not count as known.
    future = observed + ((END_UNIX - 2, Decimal("1000")),)
    assert model.probability_up(_input("100", open_price="100", observed_seconds=future, **kwargs)) == partial

    # With ten seconds left the remaining average is short, so the same spot
    # gap is sharper than the plain spot-close formula.
    twap = model.probability_up(_input("2000.20", seconds="10"))
    spot = ZeroDriftLognormalModel().probability_up(_input("2000.20", seconds="10"))
    assert twap > spot > Decimal("0.5")


def test_probability_increases_in_spot_minus_open():
    model = TwapSettlementModel()
    sigma = Decimal("0.001")
    by_spot = [
        model.probability_up(_input(str(spot), seconds="180", sigma=sigma))
        for spot in range(1980, 2030, 10)
    ]
    assert by_spot == sorted(by_spot)
    assert len(set(by_spot)) == len(by_spot)
    by_open = [
        model.probability_up(_input("2000", open_price=str(open_price), seconds="180", sigma=sigma))
        for open_price in (1990, 2000, 2010)
    ]
    assert by_open == sorted(by_open, reverse=True)
    assert by_open[0] > by_open[1] > by_open[2]


def test_tie_resolves_up_and_expiry_uses_the_realized_twap():
    model = TwapSettlementModel()
    seconds = tuple(range(END_UNIX - 62, END_UNIX - 2 + 1))
    tied = tuple((second, Decimal("100")) for second in seconds)
    decided = model.probability_up(
        _input(
            "50",
            open_price="100",
            seconds="0",
            observed_seconds=tied,
            as_of_unix=END_UNIX,
            window_end_unix=END_UNIX,
        )
    )
    assert decided == Decimal("1")
    below = tuple(
        (second, Decimal("100") if second != END_UNIX - 30 else Decimal("40")) for second in seconds
    )
    assert model.probability_up(
        _input(
            "150",
            open_price="100",
            seconds="0",
            observed_seconds=below,
            as_of_unix=END_UNIX,
            window_end_unix=END_UNIX,
        )
    ) == Decimal("0")
    # No prints at all: the current price is the whole average.
    assert model.probability_up(_input("100", open_price="100", seconds="0")) == Decimal("1")
    assert model.probability_up(_input("100", open_price="100", seconds="-5")) == Decimal("1")
    assert model.probability_up(_input("99", open_price="100", seconds="0")) == Decimal("0")
    assert model.probability_up(_input("99", open_price="100", seconds="-1", sigma=Decimal("0"))) == Decimal("0")
    # Zero volatility is a determined path, and a tie on that path is Up.
    assert model.probability_up(_input("100", open_price="100", seconds="30", sigma=Decimal("0"))) == Decimal("1")
    assert model.probability_up(_input("101", open_price="100", seconds="30", sigma=Decimal("0"))) == Decimal("1")
    assert model.probability_up(_input("99", open_price="100", seconds="30", sigma=Decimal("0"))) == Decimal("0")


def test_missing_settlement_seconds_are_forward_filled():
    model = TwapSettlementModel()
    seconds = list(range(END_UNIX - 62, END_UNIX - 2 + 1))
    missing = END_UNIX - 40
    nxt = missing + 1
    prices = {second: Decimal("100") for second in seconds}
    del prices[missing]
    prices[nxt] = Decimal("130")
    observed = tuple(sorted(prices.items()))
    common = dict(seconds="0", as_of_unix=END_UNIX, window_end_unix=END_UNIX, observed_seconds=observed)
    # Forward-fill keeps the gap at 100. The average is 100.5.
    # Filling the gap with the following print would make it 101.
    # Filling it with the current price (10) would make it 99.
    assert model.probability_up(_input("10", open_price="100.2", **common)) == Decimal("1")
    assert model.probability_up(_input("10", open_price="100.7", **common)) == Decimal("0")
    # The anchor second before the window fills a hole at the open of the window.
    first = seconds[0]
    anchored = {second: Decimal("100") for second in seconds if second != first}
    anchored[first - 1] = Decimal("100")
    assert model.probability_up(
        _input(
            "10",
            open_price="100",
            seconds="0",
            observed_seconds=tuple(sorted(anchored.items())),
            as_of_unix=END_UNIX,
            window_end_unix=END_UNIX,
        )
    ) == Decimal("1")


def test_settlement_twap_half_weights_the_blended_endpoints():
    end = 1_000_000
    prices = {second: 3.0 for second in range(end - 62, end - 2 + 1)}
    assert settlement_twap(prices, end) == pytest.approx(3.0)
    prices[end - 62] = 63.0
    assert settlement_twap(prices, end) == pytest.approx(3.5)
    del prices[end - 40]
    # Forward-filled from the previous second, which is still 3.
    assert settlement_twap(prices, end) == pytest.approx(3.5)


def test_observed_seconds_adapter_keeps_the_window_and_the_anchor():
    end = 1_800_000_000
    anchor = end - 63
    inside = end - 10
    outside = end - 80
    points = [
        (anchor * 1000, Decimal("1")),
        (inside * 1000, Decimal("2")),
        (outside * 1000, Decimal("9")),
        (inside * 1000, Decimal("4")),
    ]
    kept = dict(observed_settlement_seconds(points, end))
    assert kept == {anchor: Decimal("1"), inside: Decimal("4")}
    assert outside not in kept


def test_ewma_gives_the_newest_return_the_most_weight():
    # Half-life of one observation: weights 0.25, 0.5, 1 on the three returns.
    sigma = ewma_sigma_per_sqrt_second([0.01, -0.02, 0.03], dt_seconds=60, halflife_observations=1)
    weighted = 0.25 * 0.01**2 + 0.5 * 0.02**2 + 1.0 * 0.03**2
    assert sigma == pytest.approx((weighted / 1.75 / 60) ** 0.5)


def test_bad_prices_and_unaligned_observations_are_rejected():
    model = TwapSettlementModel()
    with pytest.raises(ValueError):
        model.probability_up(_input("0"))
    with pytest.raises(ValueError):
        model.probability_up(_input("100", open_price="0"))
    with pytest.raises(ValueError):
        model.probability_up(_input("100", sigma=Decimal("-0.1")))
    with pytest.raises(ValueError):
        model.probability_up(
            _input("100", observed_seconds=((END_UNIX, Decimal("1")),))
        )


def test_chainlink_second_prints_set_the_open_for_twap_markets():
    """The open is the 1s Chainlink average, including on eth-usd-twap-60s markets."""

    end_unix = int(END.timestamp())
    start_unix = int(START.timestamp())
    open_start = start_unix - 61
    open_end = start_unix - 2
    open_points = [
        (second * 1000, Decimal(1000 + (second - open_start)))
        for second in range(open_start, open_end + 1)
    ]
    expected_open = sum((price for _ts, price in open_points), Decimal(0)) / Decimal(60)
    base = end_unix - 45
    recent = [((base + i) * 1000, Decimal(2000 + i)) for i in range(10)]
    points = open_points + recent
    now = END - timedelta(seconds=30)
    seen = []

    def reader(spec):
        seen.append(spec)
        return points

    def transport(url, params):
        raise AssertionError("crypto-price spot endpoint must not supply the open")

    priced = ChainlinkFeed(transport=transport, reader=reader).pricing_input(make_market(), now)
    assert priced is not None
    assert priced.open_price == expected_open
    assert priced.open_price != Decimal("1990")
    assert seen[0].topic == "crypto_prices_chainlink"
    assert seen[0].symbol == "eth/usd"
    assert dict(priced.observed_seconds)[base] == Decimal(2000)
    assert base + 9 in dict(priced.observed_seconds)
    assert priced.as_of_unix == base + 9
    assert priced.window_end_unix == end_unix

    plain = replace(make_market(), resolution_source="https://data.chain.link/streams/eth-usd")
    other = ChainlinkFeed(transport=transport, reader=reader).pricing_input(plain, now)
    assert other is not None
    assert other.open_price == expected_open
    assert dict(other.observed_seconds)[base] == Decimal(2000)


def test_default_model_is_the_twap_settlement_model():
    services = default_services("paper")
    assert isinstance(services.model, TwapSettlementModel)
    assert services.feed.twap_offset_seconds == 2
    assert services.feed.min_twap_prints == 60
    assert services.feed.chainlink_spec.topic == "crypto_prices_chainlink"
    assert services.feed.chainlink_spec.symbol == "eth/usd"
