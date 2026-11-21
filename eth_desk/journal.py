from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from eth_desk.clock import iso_z
from eth_desk.redact import redact
from eth_desk.types import Fill


def _plain(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def fill_audit(
    fill: Fill,
    *,
    condition_id: str,
    slug: str,
    fee_rate: Decimal,
    fee_exponent: Decimal,
    best_ask: Decimal,
    best_ask_size: Decimal,
) -> dict[str, Any]:
    """Fields the auditor reads on every paper and live fill.

    `shares` is the filled size. `best_ask_size` is the size resting at the
    ask before the cap, which can be larger. `size` is kept as the same share
    count so older readers still see it. Nothing here is a credential.
    """

    shares = _plain(fill.size)
    return {
        "mode": fill.mode,
        "market_id": fill.market_id,
        "condition_id": condition_id,
        "slug": slug,
        "side": fill.side,
        "price": _plain(fill.price),
        "size": shares,
        "shares": shares,
        "fee": _plain(fill.fee),
        "fee_rate": _plain(fee_rate),
        "fee_exponent": _plain(fee_exponent),
        "best_ask": _plain(best_ask),
        "best_ask_size": _plain(best_ask_size),
        "fair_prob": _plain(fill.fair_prob),
        "edge": _plain(fill.edge),
        "timestamp": iso_z(fill.timestamp),
    }


class Journal:
    """Append-only JSONL log. Every record is passed through `redact` first."""

    def __init__(
        self,
        path: Path,
        secrets: tuple[str, ...] = (),
        echo: Callable[[str], None] | None = print,
    ):
        self.path = Path(path)
        self.secrets = secrets
        self.echo = echo
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> dict[str, Any]:
        clean = redact(record, self.secrets)
        line = json.dumps(clean, separators=(",", ":"), sort_keys=True, default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
        return clean

    def heartbeat(
        self,
        *,
        now: datetime,
        mode: str,
        cash: Decimal,
        daily_pnl: Decimal,
        open_positions: int,
    ) -> dict[str, Any]:
        record = self.write(
            {
                "type": "heartbeat",
                "timestamp": now.isoformat(),
                "mode": mode,
                "cash": _plain(cash),
                "daily_pnl": _plain(daily_pnl),
                "open_positions": open_positions,
            }
        )
        if self.echo is not None:
            self.echo(
                f"heartbeat {now.isoformat()} mode={mode} cash={record['cash']} "
                f"daily_pnl={record['daily_pnl']} open_positions={open_positions}"
            )
        return record

    def fill(
        self,
        fill: Fill,
        *,
        condition_id: str,
        slug: str,
        fee_rate: Decimal,
        fee_exponent: Decimal,
        best_ask: Decimal,
        best_ask_size: Decimal,
    ) -> dict[str, Any]:
        record = fill_audit(
            fill,
            condition_id=condition_id,
            slug=slug,
            fee_rate=fee_rate,
            fee_exponent=fee_exponent,
            best_ask=best_ask,
            best_ask_size=best_ask_size,
        )
        record["type"] = "fill"
        return self.write(record)

    def daily_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        record = self.write(summary)
        win_rate = record.get("win_rate")
        win_text = "n/a" if win_rate is None else win_rate
        if self.echo is not None:
            self.echo(
                f"daily_summary date={record['date']} pnl_after_fees={record['pnl_after_fees']} "
                f"fills={record['fills']} win_rate={win_text} fees_paid={record['fees_paid']}"
            )
        return record
