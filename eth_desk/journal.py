from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from eth_desk.redact import redact
from eth_desk.types import Fill


def _plain(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


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

    def fill(self, fill: Fill) -> dict[str, Any]:
        return self.write(
            {
                "type": "fill",
                "mode": fill.mode,
                "market_id": fill.market_id,
                "side": fill.side,
                "price": _plain(fill.price),
                "size": _plain(fill.size),
                "fee": _plain(fill.fee),
                "fair_prob": _plain(fill.fair_prob),
                "edge": _plain(fill.edge),
                "timestamp": fill.timestamp.isoformat(),
            }
        )

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
