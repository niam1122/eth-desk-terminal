import json
from decimal import Decimal
from io import StringIO
from pathlib import Path

from eth_desk.cli import main
from eth_desk.engine import Services, run
from eth_desk.config import Settings
from eth_desk.fees import taker_fee
from eth_desk.geoblock import GeoblockStatus
from eth_desk.journal import Journal
from eth_desk.live import LiveGateway, LiveTradingDisabled
from eth_desk.prices import WindowPrint
from eth_desk.state import DeskState
from eth_desk.types import Ask, PricingInput

from tests.helpers import END, NOW, START, make_fill, make_market, schedule


class Allowed:
    def check(self):
        return GeoblockStatus(allowed=True, blocked=False, country="DE", region="BE")


class FixedModel:
    name = "fixed"

    def __init__(self, probability: Decimal):
        self.probability = probability

    def probability_up(self, data: PricingInput) -> Decimal:
        return self.probability


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_paper_loop_buys_the_ask_up_to_book_size_and_logs_the_fill(tmp_path):
    fee_schedule = schedule(rate="0.03", rebate="0")
    market = make_market(fee_schedule=fee_schedule)

    class Gamma:
        def discover(self):
            return [market]

    class Book:
        def best_ask(self, token_id):
            if token_id != "up-token":
                return None
            return Ask(Decimal("0.40"), Decimal("8"), Decimal("5"))

    class Feed:
        def pricing_input(self, market, now):
            return PricingInput(Decimal("1560"), Decimal("1564"), Decimal("180"), Decimal("0.001"))

        def window_close(self, position, now=None):
            return None

    out = StringIO()
    log = tmp_path / "log.jsonl"
    state_path = tmp_path / "state.json"
    code = main(
        ["--once", "--log", str(log), "--state", str(state_path), "--freeze", str(tmp_path / "FREEZE")],
        services=Services(Allowed(), Gamma(), Book(), Feed(), FixedModel(Decimal("0.90")), LiveGateway(False)),
        env={"POLYMARKET_API_SECRET": "super-secret-value"},
        clock=lambda: NOW,
        stdout=out,
        stderr=StringIO(),
    )
    assert code == 0
    assert "super-secret-value" not in log.read_text()
    assert "heartbeat" in out.getvalue()
    assert "daily_summary" in out.getvalue()
    rows = _rows(log)
    fills = [row for row in rows if row["type"] == "fill"]
    assert len(fills) == 1
    fill = fills[0]
    for key in ("market_id", "side", "price", "size", "fee", "fair_prob", "edge", "timestamp"):
        assert key in fill
    assert fill["mode"] == "paper"
    assert fill["market_id"] == market.market_id
    assert fill["side"] == "Up"
    assert Decimal(fill["size"]) == Decimal("8")
    assert Decimal(fill["price"]) == Decimal("0.40")
    assert Decimal(fill["fee"]) == taker_fee(Decimal("8"), Decimal("0.40"), fee_schedule)
    assert Decimal(fill["edge"]) >= Decimal("0.06")
    rules = next(row for row in rows if row["type"] == "market_rules")
    assert rules["fee_rate"] == "0.03"
    assert rules["resolution_source"] == market.resolution_source
    saved = json.loads(state_path.read_text())
    outlay = Decimal(fill["price"]) * Decimal(fill["size"]) + Decimal(fill["fee"])
    assert outlay <= Decimal("5")
    assert Decimal(saved["cash"]) == Decimal("250") - outlay
    summary = next(row for row in rows if row["type"] == "daily_summary")
    assert summary["fills"] == 1
    assert summary["fees_paid"] == fill["fee"]
    assert summary["win_rate"] is None


def test_live_flag_is_required_and_the_stub_does_not_fill(tmp_path):
    fee_schedule = schedule(rate="0.03", rebate="0")
    market = make_market(fee_schedule=fee_schedule)
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
            return Ask(Decimal("0.40"), Decimal("8"), Decimal("5"))

    class Feed:
        def pricing_input(self, market, now):
            return PricingInput(Decimal("1560"), Decimal("1564"), Decimal("180"), Decimal("0.001"))

        def window_close(self, position, now=None):
            return None

    err = StringIO()
    code = main(
        ["--live", "--once", "--log", str(tmp_path / "a.jsonl"), "--state", str(tmp_path / "a.json"), "--freeze", str(tmp_path / "F")],
        services=Services(Allowed(), Gamma(), Book(), Feed(), FixedModel(Decimal("0.9")), LiveGateway(True, submit)),
        env={},
        clock=lambda: NOW,
        stdout=StringIO(),
        stderr=err,
    )
    assert code == 4
    assert "--live" in err.getvalue()
    assert calls == []

    env = {
        "POLYMARKET_LIVE": "1",
        "POLYMARKET_PRIVATE_KEY": "0xprivatekeyvalue",
        "POLYMARKET_API_KEY": "supersecret-key-value",
        "POLYMARKET_API_SECRET": "another-secret-value",
        "POLYMARKET_API_PASSPHRASE": "pass-phrase-value",
    }
    log = tmp_path / "live.jsonl"
    state_path = tmp_path / "live.json"
    code = main(
        ["--live", "--once", "--log", str(log), "--state", str(state_path), "--freeze", str(tmp_path / "F2")],
        services=Services(Allowed(), Gamma(), Book(), Feed(), FixedModel(Decimal("0.9")), LiveGateway(True, submit)),
        env=env,
        clock=lambda: NOW,
        stdout=StringIO(),
        stderr=StringIO(),
    )
    assert code == 0
    assert len(calls) == 1
    assert calls[0]["side"] == "Up"
    text = log.read_text()
    for secret in env.values():
        if secret != "1":
            assert secret not in text
    rows = _rows(log)
    assert not any(row["type"] == "fill" for row in rows)
    assert any(row["type"] == "live_stub" and row["sent"] is False for row in rows)
    assert json.loads(state_path.read_text())["cash"] == "250"


def test_unarmed_gateway_never_calls_submit():
    calls = []
    gateway = LiveGateway(False, lambda order: calls.append(order))
    try:
        gateway.place({"market_id": "m"})
        raised = False
    except LiveTradingDisabled:
        raised = True
    assert raised
    assert calls == []
    assert LiveGateway(True).place({"market_id": "m"})["sent"] is False


def test_settlement_uses_the_chainlink_open_and_close(tmp_path):
    state = DeskState.fresh(NOW)
    market = make_market(fee_schedule=schedule(enabled=False))
    state.commit_fill(
        make_fill(),
        slug=market.slug,
        window_end=END,
        event_start=START,
        resolution_source=market.resolution_source,
        open_price=Decimal("100"),
        order_min_size=Decimal("5"),
        now=NOW,
    )
    state.positions[0].window_end = NOW

    class Feed:
        def pricing_input(self, market, now):
            return None

        def window_close(self, position, now=None):
            return WindowPrint(Decimal("1564.18"), Decimal("1567.03"), True)

    class Gamma:
        def discover(self):
            return []

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
    run(settings, Services(Allowed(), Gamma(), object(), Feed(), FixedModel(Decimal("0.5")), LiveGateway(False)), state, journal, lambda: NOW)
    assert state.positions == []
    assert state.wins_today == 1
    assert state.daily_pnl == Decimal("5")
    assert state.cash == Decimal("255")
    settlement = next(row for row in _rows(log) if row["type"] == "settlement")
    assert settlement["won"] is True
    assert settlement["side"] == "Up"
