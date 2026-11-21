from __future__ import annotations

from dataclasses import dataclass

from eth_desk.http import HttpxJson

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


@dataclass(frozen=True)
class GeoblockStatus:
    allowed: bool
    blocked: bool | None
    country: str | None = None
    region: str | None = None
    error: str | None = None


class GeoblockRefusal(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class GeoblockClient:
    def __init__(self, transport=None, url: str = GEOBLOCK_URL):
        self.transport = transport or HttpxJson()
        self.url = url

    def check(self) -> GeoblockStatus:
        try:
            body = self.transport(self.url, None)
        except Exception as exc:
            return GeoblockStatus(
                allowed=False,
                blocked=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        return interpret_geoblock(body)


def interpret_geoblock(body: object) -> GeoblockStatus:
    if not isinstance(body, dict) or not isinstance(body.get("blocked"), bool):
        return GeoblockStatus(
            allowed=False,
            blocked=None,
            error="response missing boolean 'blocked'",
        )
    country = body.get("country")
    region = body.get("region")
    country_text = country if isinstance(country, str) else None
    region_text = region if isinstance(region, str) else None
    if body["blocked"]:
        return GeoblockStatus(
            allowed=False,
            blocked=True,
            country=country_text,
            region=region_text,
        )
    return GeoblockStatus(
        allowed=True,
        blocked=False,
        country=country_text,
        region=region_text,
    )


def refusal_message(status: GeoblockStatus) -> str:
    if status.blocked:
        country = status.country or "unknown"
        region = status.region or "unknown"
        return (
            "Refusing to run: Polymarket geoblock reports this host is blocked "
            f"(country={country}, region={region}). No orders were placed."
        )
    detail = status.error or "unrecognized geoblock response"
    return (
        "Refusing to run: Polymarket geoblock check failed "
        f"({detail}). No orders were placed."
    )
