from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from eth_desk.clock import parse_time
from eth_desk.limits import (
    BANKROLL_USD,
    DAILY_STOP_LOSS_USD,
    LAST_TRADE_SECONDS,
    MAX_BET_USD,
    MAX_BUY_PRICE,
    MAX_WINDOW_USD,
    MIN_EDGE,
    LimitBreach,
)
from eth_desk.types import Fill


@dataclass
class Position:
    market_id: str
    slug: str
    side: str
    shares: Decimal
    price: Decimal
    fee: Decimal
    cost: Decimal
    window_start: str
    window_end: datetime
    event_start: datetime
    resolution_source: str
    open_price: Decimal
    opened_at: datetime

    @property
    def outlay(self) -> Decimal:
        return self.cost + self.fee


@dataclass
class DeskState:
    cash: Decimal
    day: str
    daily_pnl: Decimal
    fees_paid_today: Decimal
    fills_today: int
    wins_today: int
    losses_today: int
    stopped_on: str | None
    window_spent: dict[str, Decimal]
    positions: list[Position] = field(default_factory=list)
    settled_pnl: Decimal = Decimal("0")

    @classmethod
    def fresh(cls, now: datetime) -> DeskState:
        return cls(
            cash=BANKROLL_USD,
            day=now.date().isoformat(),
            daily_pnl=Decimal("0"),
            fees_paid_today=Decimal("0"),
            fills_today=0,
            wins_today=0,
            losses_today=0,
            stopped_on=None,
            window_spent={},
            positions=[],
            settled_pnl=Decimal("0"),
        )

    @property
    def locked(self) -> Decimal:
        return sum((position.outlay for position in self.positions), Decimal("0"))

    def integrity_ok(self) -> bool:
        return self.cash + self.locked == BANKROLL_USD + self.settled_pnl

    def daily_stop_active(self, now: datetime) -> bool:
        today = now.date().isoformat()
        if self.day != today:
            return False
        if self.stopped_on == today:
            return True
        return self.daily_pnl <= -DAILY_STOP_LOSS_USD

    def sync_day(self, now: datetime) -> dict | None:
        """Roll the UTC day. Returns the closed day's summary the first time."""

        today = now.date().isoformat()
        if self.day == today:
            return None
        summary = self.summary()
        self.day = today
        self.daily_pnl = Decimal("0")
        self.fees_paid_today = Decimal("0")
        self.fills_today = 0
        self.wins_today = 0
        self.losses_today = 0
        self.stopped_on = None
        return summary

    def summary(self) -> dict:
        settled = self.wins_today + self.losses_today
        if settled == 0:
            win_rate = None
        else:
            win_rate = format(
                (Decimal(self.wins_today) / Decimal(settled)).quantize(Decimal("0.0001")),
                "f",
            )
        return {
            "type": "daily_summary",
            "date": self.day,
            "pnl_after_fees": format(self.daily_pnl, "f"),
            "fills": self.fills_today,
            "wins": self.wins_today,
            "losses": self.losses_today,
            "settled": settled,
            "win_rate": win_rate,
            "fees_paid": format(self.fees_paid_today, "f"),
        }

    def commit_fill(
        self,
        fill: Fill,
        *,
        slug: str,
        window_end: datetime,
        event_start: datetime,
        resolution_source: str,
        open_price: Decimal,
        order_min_size: Decimal,
        now: datetime,
    ) -> None:
        """Last gate before a paper fill changes the bankroll."""

        self.sync_day(now)
        if self.daily_stop_active(now):
            raise LimitBreach("daily_stop")
        remaining = _seconds_until(now, window_end)
        if remaining <= 0 or remaining > LAST_TRADE_SECONDS:
            raise LimitBreach("outside_last_4_minutes")
        if fill.price > MAX_BUY_PRICE or fill.price <= 0:
            raise LimitBreach("price_above_72c")
        if fill.size < order_min_size:
            raise LimitBreach("below_order_min_size")
        if fill.edge < MIN_EDGE:
            raise LimitBreach("edge_below_6_points_after_fee")
        if fill.outlay > MAX_BET_USD:
            raise LimitBreach("max_bet")
        spent = self.window_spent.get(fill.window_start, Decimal("0"))
        if spent + fill.outlay > MAX_WINDOW_USD:
            raise LimitBreach("window_cap")
        if fill.outlay > self.cash:
            raise LimitBreach("bankroll")

        self.cash -= fill.outlay
        self.window_spent[fill.window_start] = spent + fill.outlay
        self.fees_paid_today += fill.fee
        self.fills_today += 1
        self.positions.append(
            Position(
                market_id=fill.market_id,
                slug=slug,
                side=fill.side,
                shares=fill.size,
                price=fill.price,
                fee=fill.fee,
                cost=fill.cost,
                window_start=fill.window_start,
                window_end=window_end,
                event_start=event_start,
                resolution_source=resolution_source,
                open_price=open_price,
                opened_at=now,
            )
        )
        if not self.integrity_ok():
            raise LimitBreach("bankroll")

    def apply_settlement(self, position: Position, won: bool, now: datetime) -> Decimal:
        self.sync_day(now)
        if position not in self.positions:
            raise LimitBreach("unknown_position")
        payout = position.shares if won else Decimal("0")
        pnl = payout - position.cost - position.fee
        self.cash += payout
        self.settled_pnl += pnl
        self.daily_pnl += pnl
        if won:
            self.wins_today += 1
        else:
            self.losses_today += 1
        if self.daily_pnl <= -DAILY_STOP_LOSS_USD:
            self.stopped_on = self.day
        self.positions = [item for item in self.positions if item is not position]
        if not self.integrity_ok():
            raise LimitBreach("bankroll")
        return pnl

    def dump(self) -> dict:
        return {
            "bankroll_limit": format(BANKROLL_USD, "f"),
            "cash": format(self.cash, "f"),
            "day": self.day,
            "daily_pnl": format(self.daily_pnl, "f"),
            "fees_paid_today": format(self.fees_paid_today, "f"),
            "fills_today": self.fills_today,
            "wins_today": self.wins_today,
            "losses_today": self.losses_today,
            "stopped_on": self.stopped_on,
            "settled_pnl": format(self.settled_pnl, "f"),
            "window_spent": {key: format(value, "f") for key, value in self.window_spent.items()},
            "positions": [_position_dump(position) for position in self.positions],
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.dump(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> DeskState:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"state file is unreadable: {exc}") from exc
        if raw.get("bankroll_limit") != format(BANKROLL_USD, "f"):
            raise StateError("state file bankroll does not match the hard $250 limit")
        state = cls(
            cash=Decimal(raw["cash"]),
            day=raw["day"],
            daily_pnl=Decimal(raw["daily_pnl"]),
            fees_paid_today=Decimal(raw["fees_paid_today"]),
            fills_today=int(raw["fills_today"]),
            wins_today=int(raw["wins_today"]),
            losses_today=int(raw["losses_today"]),
            stopped_on=raw.get("stopped_on"),
            window_spent={key: Decimal(value) for key, value in raw.get("window_spent", {}).items()},
            positions=[_position_load(item) for item in raw.get("positions", [])],
            settled_pnl=Decimal(raw["settled_pnl"]),
        )
        if not state.integrity_ok():
            raise StateError("state file failed the bankroll integrity check; refusing to start")
        return state


class StateError(Exception):
    pass


def load_or_fresh(path: Path, now: datetime) -> DeskState:
    if not path.exists():
        return DeskState.fresh(now)
    return DeskState.load(path)


def _seconds_until(now: datetime, end: datetime) -> Decimal:
    delta = end - now
    return (
        Decimal(delta.days) * Decimal(86400)
        + Decimal(delta.seconds)
        + (Decimal(delta.microseconds) / Decimal(1_000_000))
    )


def _position_dump(position: Position) -> dict:
    return {
        "market_id": position.market_id,
        "slug": position.slug,
        "side": position.side,
        "shares": format(position.shares, "f"),
        "price": format(position.price, "f"),
        "fee": format(position.fee, "f"),
        "cost": format(position.cost, "f"),
        "window_start": position.window_start,
        "window_end": position.window_end.isoformat(),
        "event_start": position.event_start.isoformat(),
        "resolution_source": position.resolution_source,
        "open_price": format(position.open_price, "f"),
        "opened_at": position.opened_at.isoformat(),
    }


def _position_load(raw: dict) -> Position:
    return Position(
        market_id=raw["market_id"],
        slug=raw["slug"],
        side=raw["side"],
        shares=Decimal(raw["shares"]),
        price=Decimal(raw["price"]),
        fee=Decimal(raw["fee"]),
        cost=Decimal(raw["cost"]),
        window_start=raw["window_start"],
        window_end=parse_time(raw["window_end"]),
        event_start=parse_time(raw["event_start"]),
        resolution_source=raw["resolution_source"],
        open_price=Decimal(raw["open_price"]),
        opened_at=parse_time(raw["opened_at"]),
    )
