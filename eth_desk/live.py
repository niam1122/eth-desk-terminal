from __future__ import annotations

from collections.abc import Callable, Mapping

REQUIRED_LIVE_ENV = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
)


class LiveTradingDisabled(RuntimeError):
    pass


def live_armed(cli_live: bool, env: Mapping[str, str]) -> bool:
    """Live placement requires both the CLI flag and POLYMARKET_LIVE=1."""

    return bool(cli_live) and env.get("POLYMARKET_LIVE") == "1"


def missing_live_credentials(env: Mapping[str, str]) -> list[str]:
    return [name for name in REQUIRED_LIVE_ENV if not env.get(name)]


def _stub_submit(order: dict) -> dict:
    """v1 does not send orders. The stub is only reachable when live is armed."""

    return {"status": "stubbed", "sent": False, "market_id": order.get("market_id")}


class LiveGateway:
    def __init__(self, armed: bool, submit: Callable[[dict], dict] | None = None):
        self.armed = armed
        self._submit = submit if submit is not None else _stub_submit

    def place(self, order: dict) -> dict:
        if not self.armed:
            raise LiveTradingDisabled("live order placement is disabled")
        return self._submit(order)
