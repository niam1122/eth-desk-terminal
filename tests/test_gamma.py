import json
from pathlib import Path

from eth_desk.gamma import SERIES_SLUG, GammaClient, parse_market

FIXTURES = Path(__file__).parent / "fixtures"


def test_discovery_reads_rules_from_recorded_gamma_payloads():
    series = json.loads((FIXTURES / "series.json").read_text())
    events = json.loads((FIXTURES / "gamma_events.json").read_text())
    calls = []

    def transport(path, params):
        calls.append((path, params))
        if path == "/series":
            assert params["slug"] == SERIES_SLUG
            return [series]
        if path == "/events":
            assert params["series_id"] == series["id"]
            assert params["closed"] == "false"
            return events
        raise AssertionError(path)

    markets = GammaClient(transport).discover()
    assert len(markets) == 2
    first = markets[0]
    raw = events[0]["markets"][0]
    assert first.market_id == raw["id"]
    assert first.resolution_source == raw["resolutionSource"]
    assert first.order_min_size == parse_market(raw).order_min_size
    assert first.fee_schedule.rate == parse_market(raw).fee_schedule.rate
    assert first.token_ids["Up"] != first.token_ids["Down"]
    assert "Chainlink" in first.description
    assert "eth-usd-twap-60s" in first.resolution_source
    assert [path for path, _params in calls] == ["/series", "/events"]
