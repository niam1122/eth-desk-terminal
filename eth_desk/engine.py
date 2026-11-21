from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from time import sleep as default_sleep

from eth_desk.clock import iso_z, seconds_between
from eth_desk.config import Settings
from eth_desk.fees import fee_schedule_block_reason
from eth_desk.geoblock import GeoblockRefusal, refusal_message
from eth_desk.journal import Journal, fill_audit
from eth_desk.limits import LAST_TRADE_SECONDS, LimitBreach, decide_bet
from eth_desk.live import LiveGateway
from eth_desk.paper import capped_fill
from eth_desk.prices import up_wins
from eth_desk.state import DeskState
from eth_desk.types import Market


class FreezeHalt(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def freeze_engaged(path: Path) -> bool:
    return path.exists()


def freeze_message(path: Path) -> str:
    return f"FREEZE file present at {path}; stopping immediately. No order was placed."


def heartbeat_due(last: datetime | None, now: datetime, every_seconds: float) -> bool:
    if last is None:
        return True
    return (now - last).total_seconds() >= every_seconds


@dataclass
class Services:
    geoblock: object
    gamma: object
    book: object
    feed: object
    model: object
    gateway: LiveGateway


def run(settings: Settings, services: Services, state: DeskState, journal: Journal, clock, sleep=default_sleep) -> None:
    status = services.geoblock.check()
    if not status.allowed:
        raise GeoblockRefusal(refusal_message(status))

    last_heartbeat: datetime | None = None
    announced: set[str] = set()
    noted: set[str] = set()
    while True:
        now = clock()
        _halt_if_frozen(settings, journal, now)
        rolled = state.sync_day(now)
        if rolled is not None:
            journal.daily_summary(rolled)
            state.save(settings.state_path)
        if heartbeat_due(last_heartbeat, now, settings.heartbeat_seconds):
            journal.heartbeat(
                now=now,
                mode=settings.mode,
                cash=state.cash,
                daily_pnl=state.daily_pnl,
                open_positions=len(state.positions),
            )
            last_heartbeat = now
        if _settle_due(state, services, journal, now):
            state.save(settings.state_path)
        _trade(settings, services, state, journal, now, announced, noted)
        if settings.once:
            journal.daily_summary(state.summary())
            return
        sleep(settings.interval_seconds)


def _halt_if_frozen(settings: Settings, journal: Journal, now: datetime) -> None:
    if freeze_engaged(settings.freeze_path):
        journal.write(
            {
                "type": "freeze",
                "path": str(settings.freeze_path),
                "timestamp": now.isoformat(),
            }
        )
        raise FreezeHalt(freeze_message(settings.freeze_path))


def _settle_due(state: DeskState, services: Services, journal: Journal, now: datetime) -> bool:
    changed = False
    for position in list(state.positions):
        if now < position.window_end:
            continue
        try:
            printed = services.feed.window_close(position, now)
        except Exception as exc:
            journal.write(
                {
                    "type": "error",
                    "where": "settle",
                    "market_id": position.market_id,
                    "error": type(exc).__name__,
                }
            )
            continue
        if printed is None or printed.open_price is None or printed.close_price is None:
            reason = getattr(services.feed, "last_skip_reason", None)
            if reason:
                journal.write(
                    {
                        "type": "skip",
                        "where": "settle",
                        "market_id": position.market_id,
                        "slug": position.slug,
                        "side": position.side,
                        "reason": reason,
                        "timestamp": now.isoformat(),
                    }
                )
            continue
        if printed.open_price <= 0 or printed.close_price <= 0:
            continue
        settled_up = up_wins(printed.open_price, printed.close_price)
        won = settled_up if position.side == "Up" else not settled_up
        pnl = state.apply_settlement(position, won, now)
        journal.write(
            {
                "type": "settlement",
                "market_id": position.market_id,
                "side": position.side,
                "won": won,
                "pnl": format(pnl, "f"),
                "fee": format(position.fee, "f"),
                "open_price": format(printed.open_price, "f"),
                "close_price": format(printed.close_price, "f"),
                "timestamp": now.isoformat(),
            }
        )
        changed = True
    return changed


def _trade(settings, services, state, journal, now, announced: set[str], noted: set[str]) -> None:
    try:
        markets = services.gamma.discover()
    except Exception as exc:
        journal.write({"type": "error", "where": "discover", "error": type(exc).__name__})
        return
    for rejected in getattr(services.gamma, "rejections", ()) or ():
        if rejected.slug in noted:
            continue
        noted.add(rejected.slug)
        journal.write(
            {
                "type": "skip",
                "market_id": rejected.market_id,
                "slug": rejected.slug,
                "side": "",
                "reason": rejected.reason,
                "timestamp": now.isoformat(),
            }
        )
    for market in markets:
        if market.market_id not in announced:
            journal.write(_rules_record(market))
            announced.add(market.market_id)
        _consider(settings, services, state, journal, now, market, noted)


def _consider(settings, services, state, journal, now, market: Market, noted: set[str]) -> None:
    blocked = fee_schedule_block_reason(market)
    if blocked:
        if market.slug not in noted:
            _skip(journal, market, "", blocked, now)
            noted.add(market.slug)
        return
    if market.closed or not market.accepting_orders or not market.enable_order_book:
        return
    remaining = seconds_between(now, market.end)
    if remaining <= 0 or remaining > LAST_TRADE_SECONDS:
        return
    if state.daily_stop_active(now):
        _skip(journal, market, "", "daily_stop", now)
        return
    try:
        pricing = services.feed.pricing_input(market, now)
    except Exception as exc:
        journal.write(
            {
                "type": "error",
                "where": "price",
                "market_id": market.market_id,
                "error": type(exc).__name__,
                "timestamp": now.isoformat(),
            }
        )
        return
    if pricing is None:
        reason = getattr(services.feed, "last_skip_reason", None) or "no_settlement_price"
        _skip(journal, market, "", reason, now)
        return
    try:
        probability_up = services.model.probability_up(pricing)
    except Exception as exc:
        journal.write(
            {
                "type": "error",
                "where": "model",
                "market_id": market.market_id,
                "error": type(exc).__name__,
                "timestamp": now.isoformat(),
            }
        )
        return
    if not isinstance(probability_up, Decimal):
        probability_up = Decimal(str(probability_up))
    if probability_up < 0 or probability_up > 1:
        _skip(journal, market, "", "bad_fair_prob", now)
        return

    candidates = []
    for side in ("Up", "Down"):
        if side not in market.token_ids:
            continue
        fair = probability_up if side == "Up" else (Decimal("1") - probability_up)
        try:
            ask = services.book.best_ask(market.token_id(side))
        except Exception as exc:
            journal.write(
                {
                    "type": "error",
                    "where": "book",
                    "market_id": market.market_id,
                    "side": side,
                    "error": type(exc).__name__,
                    "timestamp": now.isoformat(),
                }
            )
            continue
        if ask is None:
            _skip(journal, market, side, "no_liquidity", now)
            continue
        order_min = max(market.order_min_size, ask.min_order_size)
        decision = decide_bet(
            price=ask.price,
            book_size=ask.size,
            fair_prob=fair,
            fee_schedule=market.fee_schedule,
            order_min_size=order_min,
            seconds_remaining=remaining,
            daily_pnl=state.daily_pnl,
            daily_stop_latched=state.daily_stop_active(now),
            window_spent=state.window_spent.get(iso_z(market.event_start), Decimal("0")),
            cash=state.cash,
        )
        if not decision.allowed:
            _skip(journal, market, side, decision.reason, now)
            continue
        candidates.append((decision.edge, side, decision, ask, order_min, fair))

    if not candidates:
        return
    _edge, side, decision, ask, order_min, fair = max(candidates, key=lambda item: item[0])
    fill = capped_fill(
        market_id=market.market_id,
        side=side,
        ask=ask,
        desired_shares=decision.shares,
        schedule=market.fee_schedule,
        order_min_size=order_min,
        fair_prob=fair,
        now=now,
        window_start=iso_z(market.event_start),
        mode=settings.mode,
    )
    if fill is None:
        _skip(journal, market, side, "paper_cap", now)
        return
    _halt_if_frozen(settings, journal, now)
    audit = fill_audit(
        fill,
        condition_id=market.condition_id,
        slug=market.slug,
        fee_rate=market.fee_schedule.rate,
        fee_exponent=market.fee_schedule.exponent,
        best_ask=ask.price,
        best_ask_size=ask.size,
    )
    if settings.mode == "live":
        result = services.gateway.place(
            {
                "market_id": market.market_id,
                "token_id": market.token_id(side),
                "side": side,
                "price": format(fill.price, "f"),
                "size": format(fill.size, "f"),
                "fee": format(fill.fee, "f"),
            }
        )
        sent = bool(result.get("sent"))
        journal.write({"type": "live_stub", "sent": sent, **audit})
        return
    try:
        state.commit_fill(
            fill,
            slug=market.slug,
            window_end=market.end,
            event_start=market.event_start,
            resolution_source=market.resolution_source,
            open_price=pricing.open_price,
            order_min_size=order_min,
            now=now,
        )
    except LimitBreach as exc:
        _skip(journal, market, side, exc.reason, now)
        return
    journal.fill(
        fill,
        condition_id=market.condition_id,
        slug=market.slug,
        fee_rate=market.fee_schedule.rate,
        fee_exponent=market.fee_schedule.exponent,
        best_ask=ask.price,
        best_ask_size=ask.size,
    )
    state.save(settings.state_path)


def _skip(journal: Journal, market: Market, side: str, reason: str, now: datetime) -> None:
    journal.write(
        {
            "type": "skip",
            "market_id": market.market_id,
            "slug": market.slug,
            "side": side,
            "reason": reason,
            "timestamp": now.isoformat(),
        }
    )


def _rules_record(market: Market) -> dict:
    schedule = market.fee_schedule
    return {
        "type": "market_rules",
        "market_id": market.market_id,
        "slug": market.slug,
        "resolution_source": market.resolution_source,
        "order_min_size": format(market.order_min_size, "f"),
        "fees_enabled": schedule.fees_enabled,
        "fee_rate": format(schedule.rate, "f"),
        "fee_exponent": format(schedule.exponent, "f"),
        "taker_only": schedule.taker_only,
        "rebate_rate": format(schedule.rebate_rate, "f"),
        "event_start": market.event_start.isoformat(),
        "end": market.end.isoformat(),
    }
