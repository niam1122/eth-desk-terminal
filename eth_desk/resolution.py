"""Polymarket's resolved outcome for a 15-minute ETH window.

Paper settlement pays a position from this result. It does not pay from the
terminal's own Chainlink close TWAP.

Lookup:

1. Gamma ``GET /events?slug={slug}`` supplies the market ``conditionId``.
   That call returns a market after it has closed. ``GET /markets?slug=``
   does not: a resolved ETH 15-minute market is omitted unless the query
   also passes ``closed=true``.
2. CLOB ``GET /markets/{condition_id}`` is the decision. The payload must
   have ``closed: true``, and exactly one of the Up and Down tokens must
   have ``winner: true``. That flag is what the exchange pays out. Until
   then both tokens have ``winner: false``, including while the quotes are
   already one-sided, so Gamma ``outcomePrices`` are not used as the winner.

A settlement line records ``resolution_source`` as ``clob:tokens.winner``.
An unresolved market stays open. Callers retry on their own timer.
"""

from __future__ import annotations

from dataclasses import dataclass

CLOB_WINNER_SOURCE = "clob:tokens.winner"
# The trading loop is a few seconds. Resolution is asked at this interval,
# and the unresolved log is not written more often than this either.
RESOLUTION_RETRY_SECONDS = 60
AWAITING_LOG_SECONDS = 60


class ResolutionPayloadError(ValueError):
    """Gamma or CLOB returned a payload this terminal will not settle from."""


@dataclass(frozen=True)
class Resolution:
    resolved: bool
    winner: str | None
    source: str
    condition_id: str = ""


def condition_id_for_slug(events: object, slug: str) -> str | None:
    """Condition id of `slug` inside a Gamma ``/events?slug=`` payload.

    None when the event list is empty or the market has no condition id yet.
    A payload that is not a list is refused, so a non-event body is not
    treated as "still unresolved".
    """

    if not isinstance(events, list):
        raise ResolutionPayloadError("gamma events payload was not a list")
    for event in events:
        if not isinstance(event, dict):
            continue
        markets = event.get("markets") or []
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, dict):
                continue
            if str(market.get("slug") or "") != slug:
                continue
            condition_id = str(market.get("conditionId") or "").strip()
            if condition_id:
                return condition_id
    return None


def resolution_from_clob(payload: object, slug: str, condition_id: str) -> Resolution:
    """Winner from a CLOB ``/markets/{condition_id}`` payload.

    Unresolved when the market is not closed, or when it does not have
    exactly one Up/Down token with ``winner`` true. A slug that does not
    match the requested market is refused.
    """

    if not isinstance(payload, dict):
        raise ResolutionPayloadError("clob market payload was not an object")
    reported = str(payload.get("market_slug") or payload.get("slug") or "")
    if reported and reported != slug:
        raise ResolutionPayloadError(f"clob market slug {reported} did not match {slug}")
    unresolved = Resolution(False, None, CLOB_WINNER_SOURCE, condition_id)
    if payload.get("closed") is not True:
        return unresolved
    tokens = payload.get("tokens")
    if not isinstance(tokens, list):
        return unresolved
    winners: list[str] = []
    for token in tokens:
        if not isinstance(token, dict) or token.get("winner") is not True:
            continue
        winners.append(str(token.get("outcome") or ""))
    if len(winners) != 1 or winners[0] not in {"Up", "Down"}:
        return unresolved
    return Resolution(True, winners[0], CLOB_WINNER_SOURCE, condition_id)
