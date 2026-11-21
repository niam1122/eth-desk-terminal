"""Paper and live fill records carry the auditor fields, and no secrets."""

import json
from decimal import Decimal
from io import StringIO
from pathlib import Path

from eth_desk.cli import main
from eth_desk.clock import iso_z
from eth_desk.engine import Services
from eth_desk.fees import taker_fee
from eth_desk.geoblock import GeoblockStatus
from eth_desk.journal import Journal
from eth_desk.live import LiveGateway
from eth_desk.types import Ask, PricingInput

from tests.helpers import NOW, make_fill, make_market, schedule

AUDITOR_FIELDS = (
    "condition_id",
    "slug",
    "side",
    "price",
    "shares",
    "fee",
    "fee_rate",
    "fee_exponent",
    "best_ask",
    "best_ask_size",
    "timestamp",
)


class Allowed:
    def check(self):
        return GeoblockStatus(allowed=True, blocked=False, country="DE", region="BE")


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_paper_fill_log_has_the_auditor_fields(tmp_path):
    fee_schedule = schedule(rate="0.017", exponent="1", rebate="0")
    market = make_market(fee_schedule=fee_schedule)
    secret = "super-secret-value"

    class Gamma:
        def discover(self):
            return [market]

    class Book:
        def best_ask(self, token_id):
            if token_id != "up-token":
                return None
            return Ask(Decimal("0.40"), Decimal("50"), Decimal("5"))

    class Feed:
        def pricing_input(self, market, now):
            return PricingInput(Decimal("1560"), Decimal("1564"), Decimal("180"), Decimal("0.001"))

        def window_close(self, position, now=None):
            return None

    class Model:
        name = "fixed"

        def probability_up(self, data):
            return Decimal("0.90")

    log = tmp_path / "log.jsonl"
    code = main(
        ["--once", "--log", str(log), "--state", str(tmp_path / "state.json"), "--freeze", str(tmp_path / "FREEZE")],
        services=Services(Allowed(), Gamma(), Book(), Feed(), Model(), LiveGateway(False)),
        env={"POLYMARKET_API_SECRET": secret},
        clock=lambda: NOW,
        stdout=StringIO(),
        stderr=StringIO(),
    )
    assert code == 0
    text = log.read_text()
    assert secret not in text
    fill = next(row for row in _rows(log) if row["type"] == "fill")
    for key in AUDITOR_FIELDS:
        assert key in fill
        assert fill[key] not in (None, "")
    assert fill["mode"] == "paper"
    assert fill["market_id"] == market.market_id
    assert fill["condition_id"] == market.condition_id
    assert fill["slug"] == market.slug
    assert fill["side"] == "Up"
    assert Decimal(fill["price"]) == Decimal("0.40")
    assert Decimal(fill["best_ask"]) == Decimal("0.40")
    assert Decimal(fill["best_ask_size"]) == Decimal("50")
    shares = Decimal(fill["shares"])
    assert fill["size"] == fill["shares"]
    assert shares < Decimal("50")
    assert Decimal(fill["fee"]) == taker_fee(shares, Decimal("0.40"), fee_schedule)
    assert fill["fee_rate"] == "0.017"
    assert fill["fee_exponent"] == "1"
    assert fill["timestamp"] == iso_z(NOW)
    assert fill["timestamp"].endswith("Z")
    assert "+00:00" not in fill["timestamp"]


def test_live_fill_record_and_live_attempt_use_the_same_fields(tmp_path):
    fee_schedule = schedule(rate="0.017", exponent="1", rebate="0")
    market = make_market(fee_schedule=fee_schedule)
    fill = make_fill(mode="live", price=Decimal("0.40"), size=Decimal("12.37"), fee=Decimal("0.03027"))
    journal = Journal(tmp_path / "direct.jsonl", echo=None)
    recorded = journal.fill(
        fill,
        condition_id=market.condition_id,
        slug=market.slug,
        fee_rate=fee_schedule.rate,
        fee_exponent=fee_schedule.exponent,
        best_ask=Decimal("0.40"),
        best_ask_size=Decimal("50"),
    )
    assert recorded["type"] == "fill"
    assert recorded["mode"] == "live"
    for key in AUDITOR_FIELDS:
        assert recorded[key] not in (None, "")
    assert recorded["condition_id"] == "0xabc"
    assert recorded["slug"] == market.slug
    assert recorded["side"] == "Up"
    assert recorded["fee_rate"] == "0.017"
    assert recorded["fee_exponent"] == "1"
    assert recorded["best_ask"] == "0.40"
    assert recorded["best_ask_size"] == "50"
    assert recorded["shares"] == "12.37"
    assert recorded["timestamp"] == iso_z(NOW)

    calls = []

    def submit(order):
        calls.append(order)
        return {"status": "stubbed", "sent": False}

    class Gamma:
        def discover(self):
            return [market]

    class Book:
        def best_ask(self, token_id):
            if token_id != "up-token":
                return None
            return Ask(Decimal("0.40"), Decimal("50"), Decimal("5"))

    class Feed:
        def pricing_input(self, market, now):
            return PricingInput(Decimal("1560"), Decimal("1564"), Decimal("180"), Decimal("0.001"))

        def window_close(self, position, now=None):
            return None

    class Model:
        name = "fixed"

        def probability_up(self, data):
            return Decimal("0.90")

    env = {
        "POLYMARKET_LIVE": "1",
        "POLYMARKET_PRIVATE_KEY": "0xprivatekeyvalue",
        "POLYMARKET_API_KEY": "supersecret-key-value",
        "POLYMARKET_API_SECRET": "another-secret-value",
        "POLYMARKET_API_PASSPHRASE": "pass-phrase-value",
    }
    log = tmp_path / "live.jsonl"
    code = main(
        ["--live", "--once", "--log", str(log), "--state", str(tmp_path / "state.json"), "--freeze", str(tmp_path / "FREEZE")],
        services=Services(Allowed(), Gamma(), Book(), Feed(), Model(), LiveGateway(True, submit)),
        env=env,
        clock=lambda: NOW,
        stdout=StringIO(),
        stderr=StringIO(),
    )
    assert code == 0
    assert len(calls) == 1
    text = log.read_text()
    for secret in env.values():
        if secret != "1":
            assert secret not in text
    rows = _rows(log)
    assert not any(row["type"] == "fill" for row in rows)
    attempt = next(row for row in rows if row["type"] == "live_stub")
    assert attempt["sent"] is False
    assert attempt["mode"] == "live"
    for key in AUDITOR_FIELDS:
        assert key in attempt
        assert attempt[key] not in (None, "")
    assert attempt["condition_id"] == market.condition_id
    assert attempt["slug"] == market.slug
    assert attempt["side"] == "Up"
    assert Decimal(attempt["price"]) == Decimal("0.40")
    assert Decimal(attempt["best_ask"]) == Decimal("0.40")
    assert Decimal(attempt["best_ask_size"]) == Decimal("50")
    assert Decimal(attempt["shares"]) < Decimal(attempt["best_ask_size"])
    assert attempt["fee_rate"] == "0.017"
    assert attempt["fee_exponent"] == "1"
    assert Decimal(attempt["fee"]) == taker_fee(Decimal(attempt["shares"]), Decimal("0.40"), fee_schedule)
    assert attempt["timestamp"] == iso_z(NOW)
    assert json.loads((tmp_path / "state.json").read_text())["cash"] == "250"
