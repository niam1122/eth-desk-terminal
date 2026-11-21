"""Paper settlement follows Polymarket's resolved winner, not the close TWAP."""

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest

from eth_desk.cli import main
from eth_desk.config import Settings
from eth_desk.engine import Services, run
from eth_desk.gamma import GammaClient
from eth_desk.geoblock import GeoblockStatus
from eth_desk.journal import Journal
from eth_desk.live import LiveGateway
from eth_desk.prices import ChainlinkFeed
from eth_desk.resolution import (
    CLOB_WINNER_SOURCE,
    RESOLUTION_RETRY_SECONDS,
    Resolution,
    ResolutionPayloadError,
    condition_id_for_slug,
    resolution_from_clob,
)
from eth_desk.state import DeskState

from tests.helpers import END, NOW, START, make_fill, make_market, schedule


class Allowed:
    def check(self):
        return GeoblockStatus(allowed=True, blocked=False, country="DE", region="BE")


class Done(Exception):
    pass


SLUG = "eth-updown-15m-1795257900"


def _rows(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _clob(slug: str, winner: str | None, *, closed: bool = True) -> dict:
    def token(outcome: str) -> dict:
        return {"outcome": outcome, "winner": winner == outcome, "price": 1 if winner == outcome else 0}

    return {
        "closed": closed,
        "market_slug": slug,
        "tokens": [token("Up"), token("Down")],
    }


def _commit(state: DeskState, *, side: str = "Up", open_price: str = "100") -> str:
    market = make_market(fee_schedule=schedule(enabled=False))
    state.commit_fill(
        make_fill(side=side),
        slug=market.slug,
        window_end=END,
        event_start=START,
        resolution_source=market.resolution_source,
        open_price=Decimal(open_price),
        order_min_size=Decimal("5"),
        now=NOW,
    )
    return market.slug


def _run_once(tmp_path, state, gamma, feed, when=END):
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
        Services(Allowed(), gamma, object(), feed, object(), LiveGateway(False)),
        state,
        journal,
        lambda: when,
    )
    return log


class Feed:
    def __init__(self, open_price, close_price):
        self.open_price = open_price
        self.close_price = close_price

    def settlement_prices(self, position, now=None):
        return self.open_price, self.close_price


class Resolver:
    def __init__(self, winner: str | None):
        self.winner = winner
        self.calls = []

    def discover(self):
        return []

    def resolve(self, slug):
        self.calls.append(slug)
        if self.winner is None:
            return Resolution(False, None, CLOB_WINNER_SOURCE)
        return Resolution(True, self.winner, CLOB_WINNER_SOURCE, "0xabc")


def test_clob_winner_up_means_down_lost_and_down_winner_is_down():
    up = resolution_from_clob(_clob(SLUG, "Up"), SLUG, "0x08c7")
    assert up == Resolution(True, "Up", CLOB_WINNER_SOURCE, "0x08c7")
    down = resolution_from_clob(_clob(SLUG, "Down"), SLUG, "0x08c7")
    assert down.winner == "Down"
    assert down.resolved is True


def test_open_or_ambiguous_clob_payloads_are_unresolved():
    open_market = _clob(SLUG, None, closed=False)
    open_market["tokens"] = [
        {"outcome": "Up", "winner": False, "price": 0.99},
        {"outcome": "Down", "winner": False, "price": 0.01},
    ]
    assert resolution_from_clob(open_market, SLUG, "0x1").resolved is False
    assert resolution_from_clob(_clob(SLUG, None), SLUG, "0x1").resolved is False
    both = _clob(SLUG, "Up")
    both["tokens"][1]["winner"] = True
    assert resolution_from_clob(both, SLUG, "0x1").resolved is False


def test_clob_slug_mismatch_is_refused():
    with pytest.raises(ResolutionPayloadError):
        resolution_from_clob(_clob("other-slug", "Up"), SLUG, "0x1")


def test_resolve_reads_the_condition_id_from_gamma_events_and_the_winner_from_clob():
    calls = []

    def gamma(path, params):
        calls.append((path, params))
        return [
            {
                "markets": [
                    {
                        "slug": SLUG,
                        "conditionId": "0x08c7",
                        "closed": True,
                        "umaResolutionStatus": "resolved",
                        "outcomePrices": '["1", "0"]',
                    }
                ]
            }
        ]

    def clob(path, params=None):
        calls.append((path, params))
        return _clob(SLUG, "Up")

    found = GammaClient(gamma, clob).resolve(SLUG)
    assert found == Resolution(True, "Up", CLOB_WINNER_SOURCE, "0x08c7")
    assert calls == [("/events", {"slug": SLUG}), ("/markets/0x08c7", None)]


def test_missing_event_does_not_call_clob_and_a_bad_payload_is_refused():
    def clob(path, params=None):
        raise AssertionError("clob should not be called")

    found = GammaClient(lambda path, params: [], clob).resolve(SLUG)
    assert found.resolved is False
    assert condition_id_for_slug([], SLUG) is None
    with pytest.raises(ResolutionPayloadError):
        condition_id_for_slug({"error": "nope"}, SLUG)


def test_up_winner_settles_an_up_position_and_records_the_source(tmp_path):
    state = DeskState.fresh(NOW)
    _commit(state, side="Up")
    gamma = Resolver("Up")
    log = _run_once(tmp_path, state, gamma, Feed(Decimal("100"), Decimal("110")))
    assert state.positions == []
    assert state.wins_today == 1
    assert state.daily_pnl == Decimal("5")
    assert state.cash == Decimal("255")
    rows = _rows(log)
    settlement = next(row for row in rows if row["type"] == "settlement")
    assert settlement["market_id"] == "7191736"
    assert settlement["side"] == "Up"
    assert settlement["won"] is True
    assert Decimal(settlement["pnl"]) == Decimal("5")
    assert Decimal(settlement["fee"]) == Decimal("0")
    assert Decimal(settlement["open_price"]) == Decimal("100")
    assert Decimal(settlement["close_price"]) == Decimal("110")
    assert settlement["resolution_source"] == CLOB_WINNER_SOURCE
    assert gamma.calls == ["eth-updown-15m-1795239000"]
    assert not any(row["type"] == "settlement_mismatch" for row in rows)
    assert not any(row["type"] == "awaiting_resolution" for row in rows)


def test_down_position_loses_when_polymarket_resolves_up(tmp_path):
    state = DeskState.fresh(NOW)
    _commit(state, side="Down")
    log = _run_once(tmp_path, state, Resolver("Up"), Feed(Decimal("100"), Decimal("110")))
    assert state.positions == []
    assert state.losses_today == 1
    assert state.daily_pnl == Decimal("-5")
    assert state.cash == Decimal("245")
    rows = _rows(log)
    settlement = next(row for row in rows if row["type"] == "settlement")
    assert settlement["side"] == "Down"
    assert settlement["won"] is False
    assert Decimal(settlement["pnl"]) == Decimal("-5")
    assert settlement["resolution_source"] == CLOB_WINNER_SOURCE
    assert not any(row["type"] == "settlement_mismatch" for row in rows)


def test_disagreement_with_the_close_twap_is_logged_and_does_not_decide_the_payout(tmp_path):
    state = DeskState.fresh(NOW)
    _commit(state, side="Up")
    # Close below open would be Down on our TWAP. Polymarket says Up.
    log = _run_once(tmp_path, state, Resolver("Up"), Feed(Decimal("100"), Decimal("90")))
    assert state.wins_today == 1
    assert state.cash == Decimal("255")
    rows = _rows(log)
    settlement = next(row for row in rows if row["type"] == "settlement")
    assert settlement["won"] is True
    mismatch = next(row for row in rows if row["type"] == "settlement_mismatch")
    assert mismatch["polymarket_winner"] == "Up"
    assert mismatch["twap_winner"] == "Down"
    assert Decimal(mismatch["open_price"]) == Decimal("100")
    assert Decimal(mismatch["close_price"]) == Decimal("90")
    assert mismatch["side"] == "Up"
    assert sum(row["type"] == "settlement_mismatch" for row in rows) == 1


def test_unresolved_market_is_retried_with_backoff_and_then_settles(tmp_path):
    state = DeskState.fresh(NOW)
    _commit(state, side="Up")
    offsets = [0, 5, RESOLUTION_RETRY_SECONDS - 1, RESOLUTION_RETRY_SECONDS, RESOLUTION_RETRY_SECONDS * 2]
    times = [END + timedelta(seconds=offset) for offset in offsets]
    index = {"i": 0}
    calls = []

    def clock():
        return times[index["i"]]

    class Gamma:
        def discover(self):
            return []

        def resolve(self, slug):
            now = clock()
            calls.append(now)
            if now < times[-1]:
                return Resolution(False, None, CLOB_WINNER_SOURCE)
            return Resolution(True, "Up", CLOB_WINNER_SOURCE, "0xabc")

    def sleep(_seconds):
        index["i"] += 1
        if index["i"] >= len(times):
            raise Done()

    log = tmp_path / "log.jsonl"
    journal = Journal(log, echo=None)
    settings = Settings(
        mode="paper",
        once=False,
        interval_seconds=5,
        log_path=log,
        freeze_path=tmp_path / "FREEZE",
        state_path=tmp_path / "state.json",
        heartbeat_seconds=10_000,
    )
    with pytest.raises(Done):
        run(
            settings,
            Services(Allowed(), Gamma(), object(), Feed(Decimal("100"), Decimal("110")), object(), LiveGateway(False)),
            state,
            journal,
            clock,
            sleep,
        )
    assert calls == [times[0], times[3], times[4]]
    assert state.positions == []
    rows = _rows(log)
    waiting = [row for row in rows if row["type"] == "awaiting_resolution"]
    assert len(waiting) == 2
    assert [row["timestamp"] for row in waiting] == [times[0].isoformat(), times[3].isoformat()]
    assert all(row["slug"] == "eth-updown-15m-1795239000" for row in waiting)
    settlement = next(row for row in rows if row["type"] == "settlement")
    assert settlement["won"] is True
    assert not any(row["type"] == "skip" and "twap_gap" in row.get("reason", "") for row in rows)


def test_missing_close_twap_does_not_block_settlement(tmp_path):
    start_unix = int(START.timestamp())
    end_unix = int(END.timestamp())
    open_seconds = list(range(start_unix - 61, start_unix - 2 + 1))
    close_seconds = list(range(end_unix - 61, end_unix - 2 + 1))
    prices = {second: Decimal(index) for index, second in enumerate(open_seconds, start=1)}
    for index, second in enumerate(close_seconds, start=61):
        prices[second] = Decimal(index)
    del prices[close_seconds[7]]
    points = [(second * 1000, price) for second, price in prices.items()]

    def boom(url, params):
        raise AssertionError("spot endpoint must not be read")

    feed = ChainlinkFeed(transport=boom, reader=lambda spec: points)
    state = DeskState.fresh(NOW)
    _commit(state, side="Up", open_price="999")
    log = _run_once(tmp_path, state, Resolver("Up"), feed)
    assert state.positions == []
    assert state.wins_today == 1
    rows = _rows(log)
    settlement = next(row for row in rows if row["type"] == "settlement")
    assert settlement["won"] is True
    assert Decimal(settlement["open_price"]) == Decimal("30.5")
    assert settlement["close_price"] is None
    assert settlement["resolution_source"] == CLOB_WINNER_SOURCE
    assert not any(row["type"] == "settlement_mismatch" for row in rows)
    assert not any("twap_gap" in json.dumps(row) for row in rows)


def test_persisted_open_positions_settle_on_startup_without_a_close_twap(tmp_path):
    state = DeskState.fresh(NOW)
    slug = None
    for _ in range(5):
        slug = _commit(state, side="Up", open_price="100")
    assert len(state.positions) == 5
    path = tmp_path / "state.json"
    state.save(path)
    calls = []

    class Gamma:
        def discover(self):
            return []

        def resolve(self, looked_up):
            calls.append(looked_up)
            return Resolution(True, "Up", CLOB_WINNER_SOURCE, "0xabc")

    def boom(url, params):
        raise AssertionError("spot endpoint must not be read")

    feed = ChainlinkFeed(transport=boom, reader=lambda spec: [])
    log = tmp_path / "log.jsonl"
    code = main(
        ["--once", "--log", str(log), "--state", str(path), "--freeze", str(tmp_path / "FREEZE")],
        services=Services(Allowed(), Gamma(), object(), feed, object(), LiveGateway(False)),
        env={},
        clock=lambda: END,
        stdout=StringIO(),
        stderr=StringIO(),
    )
    assert code == 0
    assert calls == [slug]
    saved = json.loads(path.read_text())
    assert saved["positions"] == []
    assert Decimal(saved["cash"]) == Decimal("275")
    assert saved["wins_today"] == 5
    rows = _rows(log)
    settlements = [row for row in rows if row["type"] == "settlement"]
    assert len(settlements) == 5
    assert all(row["won"] is True and row["resolution_source"] == CLOB_WINNER_SOURCE for row in settlements)
    assert all(row["close_price"] is None and Decimal(row["open_price"]) == Decimal("100") for row in settlements)
    assert not any(row["type"] == "settlement_mismatch" for row in rows)
    assert not any(row["type"] == "awaiting_resolution" for row in rows)
    assert not any("twap_gap" in json.dumps(row) for row in rows)
