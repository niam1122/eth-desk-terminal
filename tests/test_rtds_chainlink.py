"""Chainlink RTDS frames: real subscribe backfill, millisecond ticks, first print per second."""

import json
from decimal import Decimal
from pathlib import Path

from eth_desk.prices import (
    ChainlinkPrintBuffer,
    StreamSpec,
    boundary_twap,
    chainlink_subscribe_message,
    points_from_frame,
    points_from_message,
)

FIXTURE = Path(__file__).parent / "fixtures" / "rtds_chainlink_eth_usd.json"
SPEC = StreamSpec("crypto_prices_chainlink", "eth/usd", "ETH", "fifteen")
_E18 = Decimal(10) ** 18


def test_subscribe_frame_matches_the_live_request_and_non_json_is_ignored():
    assert chainlink_subscribe_message(SPEC) == {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": "{\"symbol\":\"eth/usd\"}",
            }
        ],
    }
    assert points_from_frame("", SPEC) == []
    assert points_from_frame("PONG", SPEC) == []
    assert points_from_frame(b"\n", SPEC) == []


def test_real_backfill_and_tick_use_unix_seconds():
    history, tick = json.loads(FIXTURE.read_text())
    # The live backfill is topic crypto_prices, not crypto_prices_chainlink.
    assert history["topic"] == "crypto_prices"
    backfill = points_from_message(history, SPEC)
    assert [second for second, _price in backfill] == [
        1795255176000,
        1795255177000,
        1795255234000,
    ]
    update = points_from_message(tick, SPEC)
    assert len(update) == 1
    timestamp_ms, price = update[0]
    assert timestamp_ms == 1795255235000
    assert price == Decimal(tick["payload"]["full_accuracy_value"]) / _E18

    buffer = ChainlinkPrintBuffer()
    buffer.extend(backfill + update)
    # A later tick in the same second must not replace the backfill, and the
    # key is the unix second, not the millisecond timestamp.
    buffer.extend([(1795255235000 + 400, Decimal("999999"))])
    prices = buffer.prices()
    assert 1795255235000 not in prices
    assert prices[1795255235] == price
    assert prices[1795255176] == backfill[0][1]
    assert set(prices) == {1795255176, 1795255177, 1795255234, 1795255235}


def test_millisecond_prints_fill_a_boundary_minute():
    boundary = 1_700_000_000
    seconds = list(range(boundary - 61, boundary - 2 + 1))
    assert len(seconds) == 60
    buffer = ChainlinkPrintBuffer()
    buffer.extend((second * 1000, Decimal(index)) for index, second in enumerate(seconds, start=1))
    buffer.extend([(seconds[0] * 1000 + 900, Decimal("999999"))])
    prices = buffer.prices()
    assert all(second < 1_000_000_000_000 for second in prices)
    assert prices[seconds[0]] == Decimal(1)
    quote = boundary_twap(prices, boundary, offset=2, min_prints=60)
    assert quote.count == 60
    assert quote.price == Decimal("30.5")
    assert quote.ready
