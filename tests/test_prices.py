import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from eth_desk.prices import (
    crypto_price_params,
    parse_price_point,
    parse_window_print,
    points_from_message,
    stream_spec,
    up_wins,
    UnrecognizedSettlementSource,
)
from eth_desk.pricing import ZeroDriftLognormalModel
from eth_desk.types import PricingInput

from tests.helpers import RESOLUTION

FIXTURES = Path(__file__).parent / "fixtures"


def test_resolution_source_selects_the_chainlink_twap_stream():
    spec = stream_spec(RESOLUTION, Decimal("900"))
    assert spec.topic == "crypto_prices_twap_sixty"
    assert spec.symbol == "eth/usd"
    assert spec.asset == "ETH"
    assert spec.variant == "fifteen"
    start = datetime(2026, 11, 21, 5, 15, tzinfo=timezone.utc)
    end = datetime(2026, 11, 21, 5, 30, tzinfo=timezone.utc)
    assert crypto_price_params(spec, start, end) == {
        "symbol": "ETH",
        "eventStartTime": "2026-11-21T05:15:00Z",
        "variant": "fifteen",
        "endDate": "2026-11-21T05:30:00Z",
    }


def test_other_sources_and_windows_are_rejected():
    with pytest.raises(UnrecognizedSettlementSource):
        stream_spec("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT", Decimal("900"))
    with pytest.raises(UnrecognizedSettlementSource):
        stream_spec("https://data.chain.link/streams/btc-usd-twap-60s-streams", Decimal("900"))
    with pytest.raises(UnrecognizedSettlementSource):
        stream_spec(RESOLUTION, Decimal("300"))


def test_recorded_window_print_resolves_up_when_close_is_above_open():
    body = json.loads((FIXTURES / "crypto_price_completed.json").read_text())
    printed = parse_window_print(body)
    assert printed.completed is True
    assert printed.open_price == Decimal("1564.1866334885142")
    assert printed.close_price == Decimal("1567.0369244753983")
    assert up_wins(printed.open_price, printed.close_price)
    assert not up_wins(Decimal("100"), Decimal("99"))
    assert up_wins(Decimal("100"), Decimal("100"))


def test_rtds_full_accuracy_is_scaled_from_1e18():
    message = json.loads((FIXTURES / "rtds_twap_update.json").read_text())
    spec = stream_spec(RESOLUTION, Decimal("900"))
    points = points_from_message(message, spec)
    assert len(points) == 1
    _timestamp, price = points[0]
    assert price == Decimal(message["payload"]["full_accuracy_value"]) / (Decimal(10) ** 18)
    assert price != Decimal(str(message["payload"]["value"]))
    plain = parse_price_point({"timestamp": 1, "full_accuracy_value": "1559.70", "value": "1"})
    assert plain[1] == Decimal("1559.70")
    other = dict(message)
    other["topic"] = "crypto_prices_chainlink"
    assert points_from_message(other, spec) == []


def test_zero_drift_model_is_replaceable_and_uses_the_four_inputs():
    model = ZeroDriftLognormalModel()
    at_the_money = model.probability_up(
        PricingInput(Decimal("100"), Decimal("100"), Decimal("120"), Decimal("0.001"))
    )
    assert Decimal("0.49") <= at_the_money <= Decimal("0.51")
    assert model.probability_up(
        PricingInput(Decimal("100"), Decimal("101"), Decimal("0"), Decimal("0.001"))
    ) == Decimal("1")
    assert model.probability_up(
        PricingInput(Decimal("100"), Decimal("99"), Decimal("0"), Decimal("0.001"))
    ) == Decimal("0")
    likely = model.probability_up(
        PricingInput(Decimal("100"), Decimal("110"), Decimal("1"), Decimal("0.0001"))
    )
    assert likely > Decimal("0.99")
    assert model.name == "zero_drift_lognormal"
