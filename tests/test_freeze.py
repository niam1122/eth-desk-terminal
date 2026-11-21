import json
from io import StringIO
from pathlib import Path

from eth_desk.cli import main
from eth_desk.engine import Services
from eth_desk.live import LiveGateway
from eth_desk.pricing import ZeroDriftLognormalModel
from eth_desk.types import PricingInput

from tests.helpers import NOW, ask, make_market


class Allowed:
    def check(self):
        from eth_desk.geoblock import GeoblockStatus

        return GeoblockStatus(allowed=True, blocked=False, country="DE", region="BE")


def _argv(tmp_path: Path, freeze: Path):
    return [
        "--once",
        "--log",
        str(tmp_path / "log.jsonl"),
        "--state",
        str(tmp_path / "state.json"),
        "--freeze",
        str(freeze),
    ]


def test_freeze_file_stops_before_discovery_or_orders(tmp_path):
    freeze = tmp_path / "FREEZE"
    freeze.write_text("halt\n")
    called = {"discover": 0, "book": 0}

    class Gamma:
        def discover(self):
            called["discover"] += 1
            return [make_market()]

    class Book:
        def best_ask(self, token_id):
            called["book"] += 1
            return ask("0.40", "20")

    err = StringIO()
    code = main(
        _argv(tmp_path, freeze),
        services=Services(Allowed(), Gamma(), Book(), object(), ZeroDriftLognormalModel(), LiveGateway(False)),
        env={},
        clock=lambda: NOW,
        stdout=StringIO(),
        stderr=err,
    )
    assert code == 3
    assert "FREEZE" in err.getvalue()
    assert "No order was placed" in err.getvalue()
    assert called == {"discover": 0, "book": 0}
    rows = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert any(row["type"] == "freeze" for row in rows)
    assert not any(row["type"] == "fill" for row in rows)


def test_freeze_appearing_before_the_fill_places_nothing(tmp_path):
    freeze = tmp_path / "FREEZE"

    class Gamma:
        def discover(self):
            return [make_market()]

    class Book:
        def best_ask(self, token_id):
            freeze.write_text("halt\n")
            return ask("0.40", "20")

    class Feed:
        def pricing_input(self, market, now):
            return PricingInput(
                open_price=__import__("decimal").Decimal("100"),
                current_price=__import__("decimal").Decimal("101"),
                seconds_remaining=__import__("decimal").Decimal("180"),
                volatility_per_sqrt_second=__import__("decimal").Decimal("0.001"),
            )

        def window_close(self, position):
            return None

    class Model:
        name = "fixed"

        def probability_up(self, data):
            return __import__("decimal").Decimal("0.90")

    err = StringIO()
    code = main(
        _argv(tmp_path, freeze),
        services=Services(Allowed(), Gamma(), Book(), Feed(), Model(), LiveGateway(False)),
        env={},
        clock=lambda: NOW,
        stdout=StringIO(),
        stderr=err,
    )
    assert code == 3
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["cash"] == "250"
    rows = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert not any(row["type"] == "fill" for row in rows)
