"""Markets with no feeSchedule, or with exponent other than 1, are not traded."""

import json
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path

from eth_desk.cli import main
from eth_desk.engine import Services
from eth_desk.fees import fee_schedule_block_reason, taker_fee
from eth_desk.gamma import GammaClient, parse_market
from eth_desk.geoblock import GeoblockStatus
from eth_desk.live import LiveGateway
from eth_desk.types import Ask, PricingInput

from tests.helpers import NOW, make_market, schedule

FIXTURES = Path(__file__).parent / "fixtures"
INSIDE = datetime(2026, 11, 21, 5, 57, tzinfo=timezone.utc)


class Allowed:
    def check(self):
        return GeoblockStatus(allowed=True, blocked=False, country="DE", region="BE")


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class _WouldTrade:
    def __init__(self):
        self.calls = []

    def pricing_input(self, market, now):
        self.calls.append("price")
        return PricingInput(Decimal("1560"), Decimal("1564"), Decimal("180"), Decimal("0.001"))

    def window_close(self, position, now=None):
        return None


class _Book:
    def __init__(self):
        self.calls = []

    def best_ask(self, token_id):
        self.calls.append(token_id)
        if token_id != "up-token":
            return None
        return Ask(Decimal("0.40"), Decimal("20"), Decimal("5"))


class _Model:
    name = "fixed"

    def probability_up(self, data):
        return Decimal("0.90")


def _run(tmp_path, services, clock, env=None):
    log = tmp_path / "log.jsonl"
    code = main(
        ["--once", "--log", str(log), "--state", str(tmp_path / "state.json"), "--freeze", str(tmp_path / "FREEZE")],
        services=services,
        env=env or {},
        clock=clock,
        stdout=StringIO(),
        stderr=StringIO(),
    )
    return code, _rows(log)


def test_missing_fee_schedule_skips_that_market_and_names_the_slug(tmp_path):
    series = json.loads((FIXTURES / "series.json").read_text())
    event = json.loads((FIXTURES / "gamma_events.json").read_text())[0]
    raw = dict(event["markets"][0])
    del raw["feeSchedule"]
    broken = dict(event)
    broken["markets"] = [raw]
    slug = raw["slug"]

    def transport(path, params):
        if path == "/series":
            return [series]
        if path == "/events":
            return [broken]
        raise AssertionError(path)

    feed = _WouldTrade()
    book = _Book()
    gamma = GammaClient(transport)
    code, rows = _run(
        tmp_path,
        Services(Allowed(), gamma, book, feed, _Model(), LiveGateway(False)),
        lambda: INSIDE,
    )
    assert code == 0
    assert feed.calls == []
    assert book.calls == []
    assert not any(row["type"] == "fill" for row in rows)
    skips = [row for row in rows if row["type"] == "skip"]
    assert len(skips) == 1
    assert skips[0]["slug"] == slug
    assert skips[0]["reason"] == f"missing feeSchedule for {slug}"
    assert slug in skips[0]["reason"]
    assert Decimal(json.loads((tmp_path / "state.json").read_text())["cash"]) == Decimal("250")


def test_exponent_two_is_parsed_then_skipped_in_paper_and_live(tmp_path):
    events = json.loads((FIXTURES / "gamma_events.json").read_text())
    raw = dict(events[0]["markets"][0])
    raw["feeSchedule"] = dict(raw["feeSchedule"], exponent=2)
    parsed = parse_market(raw)
    assert parsed is not None
    assert parsed.fee_schedule.exponent == Decimal("2")
    assert parsed.slug in fee_schedule_block_reason(parsed)

    market = make_market(fee_schedule=schedule(rate="0.01", exponent="2", rebate="0"))
    feed = _WouldTrade()
    book = _Book()

    class Gamma:
        def discover(self):
            return [market]

    code, rows = _run(
        tmp_path,
        Services(Allowed(), Gamma(), book, feed, _Model(), LiveGateway(False)),
        lambda: NOW,
    )
    assert code == 0
    assert feed.calls == []
    assert book.calls == []
    assert not any(row["type"] == "fill" for row in rows)
    skip = next(row for row in rows if row["type"] == "skip")
    assert skip["slug"] == market.slug
    assert skip["reason"] == f"feeSchedule exponent 2 != 1 for {market.slug}"

    calls = []

    def submit(order):
        calls.append(order)
        return {"status": "stubbed", "sent": False}

    live_feed = _WouldTrade()
    live_book = _Book()
    env = {
        "POLYMARKET_LIVE": "1",
        "POLYMARKET_PRIVATE_KEY": "0xprivatekeyvalue",
        "POLYMARKET_API_KEY": "supersecret-key-value",
        "POLYMARKET_API_SECRET": "another-secret-value",
        "POLYMARKET_API_PASSPHRASE": "pass-phrase-value",
    }
    live_log = tmp_path / "live"
    live_log.mkdir()
    code = main(
        [
            "--live",
            "--once",
            "--log",
            str(live_log / "log.jsonl"),
            "--state",
            str(live_log / "state.json"),
            "--freeze",
            str(live_log / "FREEZE"),
        ],
        services=Services(Allowed(), Gamma(), live_book, live_feed, _Model(), LiveGateway(True, submit)),
        env=env,
        clock=lambda: NOW,
        stdout=StringIO(),
        stderr=StringIO(),
    )
    assert code == 0
    assert calls == []
    assert live_feed.calls == []
    text = (live_log / "log.jsonl").read_text()
    assert "0xprivatekeyvalue" not in text
    assert "another-secret-value" not in text
    live_rows = _rows(live_log / "log.jsonl")
    assert not any(row["type"] == "fill" for row in live_rows)
    assert any(market.slug in row["reason"] and "exponent" in row["reason"] for row in live_rows if row["type"] == "skip")


def test_exponent_one_still_trades_using_the_schedule_rate(tmp_path):
    fee_schedule = schedule(rate="0.02", exponent="1", rebate="0")
    market = make_market(fee_schedule=fee_schedule)
    feed = _WouldTrade()
    book = _Book()

    class Gamma:
        def discover(self):
            return [market]

    code, rows = _run(
        tmp_path,
        Services(Allowed(), Gamma(), book, feed, _Model(), LiveGateway(False)),
        lambda: NOW,
    )
    assert code == 0
    assert feed.calls == ["price"]
    fills = [row for row in rows if row["type"] == "fill"]
    assert len(fills) == 1
    fill = fills[0]
    assert fill["side"] == "Up"
    assert fill["fee_exponent"] == "1"
    assert fill["fee_rate"] == "0.02"
    shares = Decimal(fill["shares"])
    assert Decimal(fill["fee"]) == taker_fee(shares, Decimal("0.40"), fee_schedule)
    assert Decimal(fill["fee"]) != taker_fee(shares, Decimal("0.40"), schedule(rate="0.01", exponent="1"))
    assert not any(row["type"] == "skip" and "feeSchedule" in row["reason"] for row in rows)
