from decimal import Decimal

from eth_desk.limits import (
    BANKROLL_USD,
    MAX_BET_USD,
    MAX_WINDOW_USD,
    REASON_BANKROLL,
    REASON_DAILY_STOP,
    REASON_EDGE,
    REASON_MAX_BET,
    REASON_MIN_SIZE,
    REASON_PRICE,
    REASON_WINDOW_CAP,
    REASON_WINDOW_TIME,
    decide_bet,
)
from eth_desk.state import DeskState, LimitBreach
from eth_desk.limits import LimitBreach as LimitBreachFromLimits

from tests.helpers import NOW, END, START, later, make_fill, make_market, passing, schedule


def test_constants_are_the_hard_limits():
    assert BANKROLL_USD == Decimal("250")
    assert MAX_BET_USD == Decimal("5")
    assert MAX_WINDOW_USD == Decimal("25")


def test_environment_cannot_raise_the_bet_cap(monkeypatch):
    monkeypatch.setenv("MAX_BET_USD", "100")
    monkeypatch.setenv("BANKROLL", "100000")
    decision = decide_bet(**passing(price=Decimal("0.50"), book_size=Decimal("1000"), fair_prob=Decimal("0.99")))
    assert decision.allowed
    assert decision.outlay <= Decimal("5")
    assert decision.outlay > Decimal("4.9")
    assert MAX_BET_USD == Decimal("5")


def test_bankroll_starts_at_250_and_blocks_a_bet_cash_cannot_cover():
    assert DeskState.fresh(NOW).cash == Decimal("250")
    blocked = decide_bet(**passing(price=Decimal("0.50"), cash=Decimal("1"), fair_prob=Decimal("0.99")))
    assert blocked.reason == REASON_BANKROLL
    allowed = decide_bet(**passing(cash=Decimal("250"), price=Decimal("0.50"), fair_prob=Decimal("0.99")))
    assert allowed.allowed
    assert allowed.outlay <= Decimal("5")


def test_commit_rejects_outlay_above_five_dollars():
    state = DeskState.fresh(NOW)
    fill = make_fill(size=Decimal("10.02"), price=Decimal("0.50"), edge=Decimal("0.30"))
    try:
        state.commit_fill(
            fill,
            slug="eth-updown-15m-1795239000",
            window_end=later(120),
            event_start=START,
            resolution_source=make_market().resolution_source,
            open_price=Decimal("100"),
            order_min_size=Decimal("5"),
            now=NOW,
        )
        raised = False
    except LimitBreachFromLimits as exc:
        raised = exc.reason == "max_bet"
    assert raised
    assert state.cash == Decimal("250")


def test_window_cap_is_25_dollars():
    blocked = decide_bet(
        **passing(window_spent=Decimal("23"), price=Decimal("0.50"), fair_prob=Decimal("0.99"))
    )
    assert blocked.reason == REASON_WINDOW_CAP
    allowed = decide_bet(
        **passing(window_spent=Decimal("20"), price=Decimal("0.50"), fair_prob=Decimal("0.99"))
    )
    assert allowed.allowed
    assert allowed.outlay <= Decimal("5")
    assert Decimal("20") + allowed.outlay <= Decimal("25")

    state = DeskState.fresh(NOW)
    market = make_market(fee_schedule=schedule(enabled=False))
    for index in range(5):
        state.commit_fill(
            make_fill(window_start="2026-11-21T05:30:00Z"),
            slug=market.slug,
            window_end=later(120),
            event_start=START,
            resolution_source=market.resolution_source,
            open_price=Decimal("100"),
            order_min_size=Decimal("5"),
            now=NOW,
        )
    assert state.window_spent["2026-11-21T05:30:00Z"] == Decimal("25")
    assert state.cash == Decimal("225")
    try:
        state.commit_fill(
            make_fill(),
            slug=market.slug,
            window_end=later(120),
            event_start=START,
            resolution_source=market.resolution_source,
            open_price=Decimal("100"),
            order_min_size=Decimal("5"),
            now=NOW,
        )
        raised = False
    except LimitBreach as exc:
        raised = exc.reason == "window_cap"
    assert raised
    assert state.cash == Decimal("225")


def test_never_buy_above_72_cents():
    assert decide_bet(**passing(price=Decimal("0.72"), fair_prob=Decimal("0.99"))).allowed
    assert decide_bet(**passing(price=Decimal("0.721"), fair_prob=Decimal("0.99"))).reason == REASON_PRICE
    assert decide_bet(**passing(price=Decimal("0.73"), fair_prob=Decimal("0.99"))).reason == REASON_PRICE


def test_only_the_last_four_minutes_are_tradeable():
    assert decide_bet(**passing(seconds_remaining=Decimal("240"))).allowed
    assert decide_bet(**passing(seconds_remaining=Decimal("241"))).reason == REASON_WINDOW_TIME
    assert decide_bet(**passing(seconds_remaining=Decimal("900"))).reason == REASON_WINDOW_TIME
    assert decide_bet(**passing(seconds_remaining=Decimal("0"))).reason == REASON_WINDOW_TIME
    assert decide_bet(**passing(seconds_remaining=Decimal("-1"))).reason == REASON_WINDOW_TIME
    assert END > NOW


def test_daily_stop_latches_for_the_rest_of_the_utc_day():
    assert decide_bet(**passing(daily_pnl=Decimal("-14.99"))).allowed
    assert decide_bet(**passing(daily_pnl=Decimal("-15"))).reason == REASON_DAILY_STOP
    assert decide_bet(**passing(daily_pnl=Decimal("-15.01"))).reason == REASON_DAILY_STOP
    assert decide_bet(**passing(daily_stop_latched=True, daily_pnl=Decimal("0"))).reason == REASON_DAILY_STOP

    state = DeskState.fresh(NOW)
    market = make_market()
    for _ in range(3):
        state.commit_fill(
            make_fill(window_start=f"w{state.fills_today}"),
            slug=market.slug,
            window_end=later(120),
            event_start=START,
            resolution_source=market.resolution_source,
            open_price=Decimal("100"),
            order_min_size=Decimal("5"),
            now=NOW,
        )
        pnl = state.apply_settlement(state.positions[-1], won=False, now=NOW)
        assert pnl == Decimal("-5")
    assert state.daily_pnl == Decimal("-15")
    assert state.daily_stop_active(NOW)
    assert state.daily_stop_active(later(3600))
    tomorrow = later(24 * 3600)
    summary = state.sync_day(tomorrow)
    assert summary["date"] == "2026-11-21"
    assert Decimal(summary["pnl_after_fees"]) == Decimal("-15")
    assert summary["fills"] == 3
    assert summary["losses"] == 3
    assert summary["win_rate"] == "0.0000"
    assert Decimal(summary["fees_paid"]) == Decimal("0")
    assert not state.daily_stop_active(tomorrow)


def test_edge_must_clear_six_points_after_the_supplied_fee():
    fee_schedule = schedule(rate="0.2", rebate="0")
    price = Decimal("0.40")
    shares = Decimal("10")
    from eth_desk.fees import taker_fee

    per_share = taker_fee(shares, price, fee_schedule) / shares
    common = dict(price=price, book_size=shares, fee_schedule=fee_schedule)
    short = decide_bet(**passing(**common, fair_prob=price + Decimal("0.06")))
    assert short.reason == REASON_EDGE
    cleared = decide_bet(**passing(**common, fair_prob=price + per_share + Decimal("0.06")))
    assert cleared.allowed
    assert cleared.shares == shares
    assert cleared.edge >= Decimal("0.06")


def test_order_min_size_blocks_a_book_that_cannot_fill_it():
    decision = decide_bet(**passing(book_size=Decimal("4"), order_min_size=Decimal("5")))
    assert decision.reason == REASON_MIN_SIZE
    allowed = decide_bet(**passing(book_size=Decimal("5"), order_min_size=Decimal("5")))
    assert allowed.allowed
    assert allowed.shares >= Decimal("5")
