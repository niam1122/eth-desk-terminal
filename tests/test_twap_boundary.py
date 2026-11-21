"""Settlement open/close is the plain 60-second Chainlink average, not the spot print."""

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from eth_desk.cli import main
from eth_desk.clock import iso_z
from eth_desk.config import Settings
from eth_desk.engine import Services, run
from eth_desk.geoblock import GeoblockStatus
from eth_desk.journal import Journal
from eth_desk.live import LiveGateway
from eth_desk.prices import (
    ChainlinkFeed,
    FilledPrint,
    StreamSpec,
    boundary_twap,
    points_from_message,
    twap_gap_reason,
)
from eth_desk.resolution import CLOB_WINNER_SOURCE, Resolution
from eth_desk.state import DeskState
from eth_desk.types import Ask

from tests.helpers import END, NOW, START, make_fill, make_market, schedule


class Allowed:
    def check(self):
        return GeoblockStatus(allowed=True, blocked=False, country="DE", region="BE")


def _mean(values) -> Decimal:
    items = list(values)
    return sum(items, Decimal("0")) / Decimal(len(items))


def test_exact_average_of_synthetic_prints():
    boundary = 10_000
    prices = {second: Decimal(second) for second in range(boundary - 61, boundary - 2 + 1)}
    assert len(prices) == 60
    quote = boundary_twap(prices, boundary, offset=2, min_prints=60)
    assert quote.count == 60
    assert quote.start == boundary - 61
    assert quote.end == boundary - 2
    assert quote.price == _mean(prices.values())
    assert quote.price == Decimal(boundary) - Decimal("31.5")
    # 1..60 shifted onto the window is the same shape: mean is exact, not a float.
    unit = {second: Decimal(index) for index, second in enumerate(range(quote.start, quote.end + 1), start=1)}
    unit_quote = boundary_twap(unit, boundary, offset=2, min_prints=60)
    assert unit_quote.price == Decimal("30.5")


def test_offset_moves_the_window_and_prints_outside_it_do_not_count():
    boundary = 1_700_000_000
    prices = {second: Decimal(second - (boundary - 70)) for second in range(boundary - 70, boundary + 1)}
    offset_2 = boundary_twap(prices, boundary, offset=2, min_prints=60)
    offset_3 = boundary_twap(prices, boundary, offset=3, min_prints=60)
    assert (offset_2.start, offset_2.end) == (boundary - 61, boundary - 2)
    assert (offset_3.start, offset_3.end) == (boundary - 62, boundary - 3)
    assert offset_2.price == _mean(prices[second] for second in range(offset_2.start, offset_2.end + 1))
    assert offset_3.price == _mean(prices[second] for second in range(offset_3.start, offset_3.end + 1))
    assert offset_2.price != offset_3.price

    spiked = dict(prices)
    spiked[boundary - 62] = Decimal("999999")
    spiked[boundary - 1] = Decimal("999999")
    spiked[boundary] = Decimal("999999")
    assert boundary_twap(spiked, boundary, offset=2, min_prints=60).price == offset_2.price

    # The feed's configured offset is the one used for the open the model sees.
    now = START + timedelta(seconds=30)
    grid_seconds = range(int(START.timestamp()) - 80, int(now.timestamp()) + 1)
    points = [(second * 1000, Decimal(second)) for second in grid_seconds]
    keyed = {second: Decimal(second) for second in grid_seconds}

    def priced(offset):
        def boom(url, params):
            raise AssertionError("spot endpoint must not be read")

        feed = ChainlinkFeed(
            transport=boom,
            reader=lambda spec: points,
            twap_offset_seconds=offset,
        )
        result = feed.pricing_input(make_market(), now)
        assert result is not None
        assert feed.chainlink_spec == StreamSpec("crypto_prices_chainlink", "eth/usd", "ETH", "fifteen")
        return result.open_price

    assert priced(2) == boundary_twap(keyed, int(START.timestamp()), offset=2).price
    assert priced(3) == boundary_twap(keyed, int(START.timestamp()), offset=3).price
    assert priced(2) != priced(3)


def test_chainlink_topic_is_the_one_second_source():
    spec = StreamSpec("crypto_prices_chainlink", "eth/usd", "ETH", "fifteen")
    message = {
        "topic": "crypto_prices_chainlink",
        "payload": {"symbol": "eth/usd", "timestamp": 1_700_000_000_000, "value": "2000.5"},
    }
    assert points_from_message(message, spec) == [(1_700_000_000_000, Decimal("2000.5"))]
    other = dict(message)
    other["topic"] = "crypto_prices_twap_sixty"
    assert points_from_message(other, spec) == []


def test_gap_skips_the_window_and_logs_the_reason(tmp_path):
    start_unix = int(START.timestamp())
    window = list(range(start_unix - 61, start_unix - 2 + 1))
    assert len(window) == 60
    missing = {window[10], window[25]}
    points = [(second * 1000, Decimal("2000")) for second in window if second not in missing]
    points.append((int(NOW.timestamp()) * 1000, Decimal("2001")))
    points.append(((int(NOW.timestamp()) - 1) * 1000, Decimal("2002")))
    seen = []

    def reader(spec):
        seen.append(spec)
        return points

    def boom(url, params):
        raise AssertionError("spot endpoint must not be read")

    feed = ChainlinkFeed(transport=boom, reader=reader)
    market = make_market()
    asked = []

    class Gamma:
        def discover(self):
            return [market]

    class Book:
        def best_ask(self, token_id):
            asked.append(token_id)
            return Ask(Decimal("0.40"), Decimal("20"), Decimal("5"))

    class Model:
        name = "fixed"

        def probability_up(self, data):
            raise AssertionError("a gapped window must not be priced")

    log = tmp_path / "log.jsonl"
    code = main(
        ["--once", "--log", str(log), "--state", str(tmp_path / "state.json"), "--freeze", str(tmp_path / "FREEZE")],
        services=Services(Allowed(), Gamma(), Book(), feed, Model(), LiveGateway(False)),
        env={},
        clock=lambda: NOW,
        stdout=StringIO(),
        stderr=StringIO(),
    )
    assert code == 0
    assert seen and seen[0].topic == "crypto_prices_chainlink"
    assert asked == []
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert not any(row["type"] == "fill" for row in rows)
    skips = [row for row in rows if row["type"] == "skip" and "twap_gap" in row["reason"]]
    assert len(skips) == 1
    assert "58 of 60" in skips[0]["reason"]
    assert "twap_gap open" in skips[0]["reason"]
    assert market.slug in skips[0]["reason"]
    assert skips[0]["slug"] == market.slug
    assert Decimal(json.loads((tmp_path / "state.json").read_text())["cash"]) == Decimal("250")


def test_model_is_given_the_twap_open(tmp_path):
    start_unix = int(START.timestamp())
    window = list(range(start_unix - 61, start_unix - 2 + 1))
    prices = {second: Decimal(index) for index, second in enumerate(window, start=1)}
    expected = _mean(prices.values())
    assert expected == Decimal("30.5")
    points = [(second * 1000, price) for second, price in prices.items()]
    now_unix = int(NOW.timestamp())
    points.extend((second * 1000, Decimal(40 + (now_unix - second))) for second in range(now_unix - 12, now_unix + 1))
    market = make_market()
    seen = []

    class Gamma:
        def discover(self):
            return [market]

    class Book:
        def best_ask(self, token_id):
            if token_id != "up-token":
                return None
            return Ask(Decimal("0.40"), Decimal("20"), Decimal("5"))

    class Model:
        name = "fixed"

        def probability_up(self, data):
            seen.append(data.open_price)
            return Decimal("0.90")

    def boom(url, params):
        raise AssertionError("spot endpoint must not be read")

    feed = ChainlinkFeed(transport=boom, reader=lambda spec: points)
    log = tmp_path / "log.jsonl"
    code = main(
        ["--once", "--log", str(log), "--state", str(tmp_path / "state.json"), "--freeze", str(tmp_path / "FREEZE")],
        services=Services(Allowed(), Gamma(), Book(), feed, Model(), LiveGateway(False)),
        env={},
        clock=lambda: NOW,
        stdout=StringIO(),
        stderr=StringIO(),
    )
    assert code == 0
    assert seen == [expected]
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert any(row["type"] == "fill" for row in rows)
    assert not any(row["type"] == "skip" and "twap_gap" in row.get("reason", "") for row in rows)
    assert not any(row["type"] == "twap_fill" for row in rows)


def test_paper_settlement_uses_twap_close_versus_twap_open(tmp_path):
    start_unix = int(START.timestamp())
    end_unix = int(END.timestamp())
    open_seconds = list(range(start_unix - 61, start_unix - 2 + 1))
    close_seconds = list(range(end_unix - 61, end_unix - 2 + 1))
    assert len(open_seconds) == 60 and len(close_seconds) == 60
    prices = {}
    for index, second in enumerate(open_seconds, start=1):
        prices[second] = Decimal(index)
    for index, second in enumerate(close_seconds, start=61):
        prices[second] = Decimal(index)
    # Boundary spots would settle the other way: 100000 then 0.01 is Down.
    prices[start_unix] = Decimal("100000")
    prices[start_unix - 62] = Decimal("100000")
    prices[end_unix] = Decimal("0.01")
    prices[end_unix - 1] = Decimal("0.01")
    points = [(second * 1000, price) for second, price in prices.items()]
    seen = []

    def reader(spec):
        seen.append(spec)
        return points

    def boom(url, params):
        raise AssertionError("spot endpoint must not settle the window")

    feed = ChainlinkFeed(transport=boom, reader=reader)
    state = DeskState.fresh(NOW)
    market = make_market(fee_schedule=schedule(enabled=False))
    state.commit_fill(
        make_fill(),
        slug=market.slug,
        window_end=END,
        event_start=START,
        resolution_source=market.resolution_source,
        open_price=Decimal("999"),
        order_min_size=Decimal("5"),
        now=NOW,
    )

    class Gamma:
        def discover(self):
            return []

        def resolve(self, slug):
            return Resolution(True, "Up", CLOB_WINNER_SOURCE, "0xabc")

    log = tmp_path / "log.jsonl"
    journal = Journal(log, echo=None)
    settings = Settings(
        mode="paper",
        once=True,
        interval_seconds=5,
        log_path=log,
        freeze_path=tmp_path / "FREEZE",
        state_path=tmp_path / "state.json",
    )
    run(
        settings,
        Services(Allowed(), Gamma(), object(), feed, object(), LiveGateway(False)),
        state,
        journal,
        lambda: END,
    )
    assert seen and seen[0].topic == "crypto_prices_chainlink"
    assert state.positions == []
    assert state.wins_today == 1
    settlement = next(row for row in (json.loads(line) for line in log.read_text().splitlines()) if row["type"] == "settlement")
    assert Decimal(settlement["open_price"]) == Decimal("30.5")
    assert Decimal(settlement["close_price"]) == Decimal("90.5")
    assert settlement["open_price"] != "999"
    assert settlement["won"] is True
    assert settlement["side"] == "Up"
    assert settlement["resolution_source"] == CLOB_WINNER_SOURCE
    assert not any(
        row["type"] == "settlement_mismatch"
        for row in (json.loads(line) for line in log.read_text().splitlines())
    )


def _open_window() -> tuple[int, list[int]]:
    start_unix = int(START.timestamp())
    window = list(range(start_unix - 61, start_unix - 2 + 1))
    assert len(window) == 60
    return start_unix, window


def _numbered(window: list[int]) -> dict[int, Decimal]:
    return {second: Decimal(index) for index, second in enumerate(window, start=1)}


def test_complete_open_minute_is_unchanged_when_fill_is_allowed():
    start_unix, window = _open_window()
    prices = _numbered(window)
    plain = boundary_twap(prices, start_unix, offset=2, min_prints=60)
    filled = boundary_twap(prices, start_unix, offset=2, min_prints=60, fill_one=True)
    assert plain.price == Decimal("30.5")
    assert filled.price == plain.price
    assert filled.count == 60
    assert filled.fills == ()


def test_one_missing_open_print_is_forward_filled_from_the_previous_second():
    start_unix, window = _open_window()
    prices = _numbered(window)
    missing = window[10]
    previous = window[9]
    assert prices[previous] != prices[window[11]]
    del prices[missing]
    quote = boundary_twap(prices, start_unix, offset=2, min_prints=60, fill_one=True)
    assert quote.fills == (FilledPrint(missing, prices[previous]),)
    assert quote.count == 60
    expected = dict(_numbered(window))
    expected[missing] = prices[previous]
    assert quote.price == _mean(expected.values())
    raw = _mean(price for second, price in _numbered(window).items() if second != missing)
    assert quote.price != raw


def test_two_missing_open_prints_stay_a_gap():
    start_unix, window = _open_window()
    prices = _numbered(window)
    del prices[window[10]]
    del prices[window[25]]
    quote = boundary_twap(prices, start_unix, offset=2, min_prints=60, fill_one=True)
    assert quote.price is None
    assert quote.count == 58
    assert quote.fills == ()
    reason = twap_gap_reason("open", quote, "eth-updown-15m-1795239000")
    assert reason.startswith("twap_gap open: 58 of 60 ")
    assert "eth-updown-15m-1795239000" in reason


def test_missing_first_second_is_filled_from_the_next_value_not_the_second_before_the_window():
    start_unix, window = _open_window()
    prices = _numbered(window)
    first = window[0]
    nxt = window[1]
    outside = first - 1
    prices[outside] = Decimal("999999")
    del prices[first]
    quote = boundary_twap(prices, start_unix, offset=2, min_prints=60, fill_one=True)
    assert quote.fills == (FilledPrint(first, prices[nxt]),)
    assert quote.fills[0].value != Decimal("999999")
    expected = dict(_numbered(window))
    expected[first] = prices[nxt]
    assert quote.price == _mean(expected.values())

    points = [(second * 1000, price) for second, price in prices.items() if second != outside]
    points.append((outside * 1000, Decimal("999999")))
    now_unix = int(NOW.timestamp())
    points.extend((second * 1000, Decimal(40 + (now_unix - second))) for second in range(now_unix - 12, now_unix + 1))

    def boom(url, params):
        raise AssertionError("spot endpoint must not be read")

    feed = ChainlinkFeed(transport=boom, reader=lambda spec: points)
    market = make_market()
    assert feed.pricing_input(market, NOW) is not None
    assert feed.pricing_input(market, NOW) is not None
    notes = [row for row in feed.drain_notes() if row["type"] == "twap_fill"]
    assert len(notes) == 1
    assert notes[0]["slug"] == market.slug
    assert notes[0]["boundary"] == iso_z(START)
    assert notes[0]["missing_second"] == first
    assert Decimal(notes[0]["filled_value"]) == prices[nxt]


def test_one_missing_open_print_is_logged_and_priced(tmp_path):
    start_unix, window = _open_window()
    prices = _numbered(window)
    missing = window[10]
    previous = prices[window[9]]
    del prices[missing]
    expected = dict(_numbered(window))
    expected[missing] = previous
    expected_open = _mean(expected.values())
    points = [(second * 1000, price) for second, price in prices.items()]
    now_unix = int(NOW.timestamp())
    points.extend((second * 1000, Decimal(40 + (now_unix - second))) for second in range(now_unix - 12, now_unix + 1))
    market = make_market()
    seen = []

    class Gamma:
        def discover(self):
            return [market]

    class Book:
        def best_ask(self, token_id):
            if token_id != "up-token":
                return None
            return Ask(Decimal("0.40"), Decimal("20"), Decimal("5"))

    class Model:
        name = "fixed"

        def probability_up(self, data):
            seen.append(data.open_price)
            return Decimal("0.90")

    def boom(url, params):
        raise AssertionError("spot endpoint must not be read")

    feed = ChainlinkFeed(transport=boom, reader=lambda spec: points)
    log = tmp_path / "log.jsonl"
    code = main(
        ["--once", "--log", str(log), "--state", str(tmp_path / "state.json"), "--freeze", str(tmp_path / "FREEZE")],
        services=Services(Allowed(), Gamma(), Book(), feed, Model(), LiveGateway(False)),
        env={},
        clock=lambda: NOW,
        stdout=StringIO(),
        stderr=StringIO(),
    )
    assert code == 0
    assert seen == [expected_open]
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    fills = [row for row in rows if row["type"] == "twap_fill"]
    assert len(fills) == 1
    assert fills[0]["slug"] == market.slug
    assert fills[0]["boundary"] == iso_z(START)
    assert fills[0]["missing_second"] == missing
    assert Decimal(fills[0]["filled_value"]) == previous
    assert not any(row["type"] == "skip" and "twap_gap" in row.get("reason", "") for row in rows)
