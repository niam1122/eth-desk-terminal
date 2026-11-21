from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from eth_desk.clock import parse_time
from eth_desk.fees import MissingFeeSchedule
from eth_desk.http import HttpxJson
from eth_desk.types import FeeSchedule, Market, as_decimal

GAMMA_HOST = "https://gamma-api.polymarket.com"
SERIES_SLUG = "eth-up-or-down-15m"
SLUG_PREFIX = "eth-updown-15m-"


class MarketRulesError(ValueError):
    """A market looks like ETH 15m but its rules could not be read."""


class GammaClient:
    def __init__(self, transport=None):
        self.transport = transport or HttpxJson(GAMMA_HOST)

    def discover(self) -> list[Market]:
        series_rows = self.transport("/series", {"slug": SERIES_SLUG})
        if not isinstance(series_rows, list) or not series_rows:
            raise MarketRulesError(f"gamma series {SERIES_SLUG} was not found")
        series_id = series_rows[0].get("id")
        if not series_id:
            raise MarketRulesError("gamma series payload had no id")
        events = self.transport(
            "/events",
            {
                "series_id": str(series_id),
                "closed": "false",
                "limit": "20",
                "order": "endDate",
                "ascending": "true",
            },
        )
        if not isinstance(events, list):
            raise MarketRulesError("gamma events payload was not a list")
        markets: list[Market] = []
        for event in events:
            for raw in event.get("markets") or []:
                parsed = parse_market(raw)
                if parsed is not None:
                    markets.append(parsed)
        return markets


def parse_market(raw: dict[str, Any]) -> Market | None:
    """Parse one gamma market. Returns None when it is not an ETH 15m up/down."""

    slug = str(raw.get("slug") or "")
    if not slug.startswith(SLUG_PREFIX):
        return None
    outcomes = _as_list(raw.get("outcomes"), "outcomes")
    if tuple(outcomes) != ("Up", "Down"):
        raise MarketRulesError(f"{slug} outcomes were {outcomes!r}, expected Up/Down")
    token_ids = _as_list(raw.get("clobTokenIds"), "clobTokenIds")
    if len(token_ids) != len(outcomes):
        raise MarketRulesError(f"{slug} token ids do not line up with outcomes")
    resolution_source = raw.get("resolutionSource") or ""
    if "chain.link" not in resolution_source or "eth-usd" not in resolution_source:
        raise MarketRulesError(f"{slug} resolutionSource is not a Chainlink ETH/USD stream")
    if "orderMinSize" not in raw:
        raise MarketRulesError(f"{slug} is missing orderMinSize")
    schedule = parse_fee_schedule(raw, slug)
    event_start = parse_time(str(raw["eventStartTime"]))
    end = parse_time(str(raw["endDate"]))
    return Market(
        market_id=str(raw.get("id") or ""),
        condition_id=str(raw.get("conditionId") or ""),
        slug=slug,
        question=str(raw.get("question") or ""),
        description=str(raw.get("description") or ""),
        resolution_source=str(resolution_source),
        event_start=event_start,
        end=end,
        outcomes=tuple(str(item) for item in outcomes),
        token_ids={str(side): str(token) for side, token in zip(outcomes, token_ids)},
        order_min_size=as_decimal(raw["orderMinSize"]),
        tick_size=as_decimal(raw.get("orderPriceMinTickSize") or "0.01"),
        fee_schedule=schedule,
        accepting_orders=bool(raw.get("acceptingOrders")),
        closed=bool(raw.get("closed")),
        enable_order_book=bool(raw.get("enableOrderBook")),
    )


def parse_fee_schedule(raw: dict[str, Any], slug: str) -> FeeSchedule:
    if "feesEnabled" not in raw:
        raise MarketRulesError(f"{slug} is missing feesEnabled")
    enabled = bool(raw.get("feesEnabled"))
    schedule = raw.get("feeSchedule")
    if not isinstance(schedule, dict):
        raise MissingFeeSchedule(f"{slug} is missing feeSchedule")
    if enabled and ("rate" not in schedule or "exponent" not in schedule):
        raise MissingFeeSchedule(f"{slug} feeSchedule is missing rate or exponent")
    rate = as_decimal(schedule["rate"]) if "rate" in schedule else Decimal("0")
    exponent = as_decimal(schedule["exponent"]) if "exponent" in schedule else Decimal("0")
    rebate = as_decimal(schedule["rebateRate"]) if schedule.get("rebateRate") is not None else Decimal("0")
    return FeeSchedule(
        exponent=exponent,
        rate=rate,
        taker_only=bool(schedule.get("takerOnly", True)),
        rebate_rate=rebate,
        fees_enabled=enabled,
    )


def _as_list(value: Any, field: str) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    raise MarketRulesError(f"{field} was not a list")
