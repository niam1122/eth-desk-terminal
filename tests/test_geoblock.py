import json
from io import StringIO
from pathlib import Path

from eth_desk.cli import main
from eth_desk.engine import Services
from eth_desk.geoblock import GEOBLOCK_URL, GeoblockClient, interpret_geoblock
from eth_desk.live import LiveGateway

FIXTURES = Path(__file__).parent / "fixtures"


def _services(transport) -> Services:
    class Gamma:
        def discover(self):
            raise AssertionError("discovery must not run when geoblock refuses")

    return Services(
        geoblock=GeoblockClient(transport),
        gamma=Gamma(),
        book=object(),
        feed=object(),
        model=object(),
        gateway=LiveGateway(False),
    )


def _argv(tmp_path: Path, name: str) -> list[str]:
    return [
        "--once",
        "--log",
        str(tmp_path / f"{name}.jsonl"),
        "--state",
        str(tmp_path / f"{name}.json"),
        "--freeze",
        str(tmp_path / f"FREEZE-{name}"),
    ]


def test_geoblock_url_is_the_official_endpoint():
    assert GEOBLOCK_URL == "https://polymarket.com/api/geoblock"


def test_recorded_blocked_response_exits_nonzero(tmp_path):
    recorded = json.loads((FIXTURES / "geoblock_blocked.json").read_text())
    assert recorded == {"blocked": True, "country": "US", "region": "VA"}
    err = StringIO()
    code = main(
        _argv(tmp_path, "blocked"),
        services=_services(lambda url, params: recorded),
        env={},
        stdout=StringIO(),
        stderr=err,
    )
    assert code == 2
    message = err.getvalue()
    assert "blocked" in message
    assert "US" in message
    assert "No orders were placed" in message
    assert not (tmp_path / "blocked.jsonl").exists()


def test_failed_geoblock_check_exits_nonzero(tmp_path):
    def fail(url, params):
        raise ConnectionError("network down")

    err = StringIO()
    code = main(
        _argv(tmp_path, "down"),
        services=_services(fail),
        env={},
        stdout=StringIO(),
        stderr=err,
    )
    assert code == 2
    text = err.getvalue()
    assert "failed" in text
    assert "ConnectionError" in text
    assert "No orders were placed" in text


def test_missing_or_non_boolean_blocked_is_a_failed_check():
    assert interpret_geoblock({"country": "US"}).allowed is False
    assert interpret_geoblock({"blocked": "false"}).allowed is False
    allowed = interpret_geoblock({"blocked": False, "country": "DE", "region": "BE"})
    assert allowed.allowed is True
    assert allowed.blocked is False
