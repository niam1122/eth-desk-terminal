from __future__ import annotations

from decimal import Decimal

from eth_desk.types import Ask

CLOB_HOST = "https://clob.polymarket.com"


class ClobBook:
    """Best ask via the official py-clob-client. Read-only unless a caller posts."""

    def __init__(self, client=None, host: str = CLOB_HOST):
        if client is None:
            from py_clob_client.client import ClobClient

            client = ClobClient(host)
        self.client = client

    def best_ask(self, token_id: str) -> Ask | None:
        book = self.client.get_order_book(token_id)
        levels = []
        for level in book.asks or []:
            price = Decimal(str(level.price))
            size = Decimal(str(level.size))
            if size > 0 and price > 0:
                levels.append((price, size))
        if not levels:
            return None
        best = min(price for price, _size in levels)
        size = sum((level_size for price, level_size in levels if price == best), Decimal("0"))
        min_size = (
            Decimal(str(book.min_order_size))
            if book.min_order_size not in (None, "")
            else Decimal("0")
        )
        tick = Decimal(str(book.tick_size)) if book.tick_size not in (None, "") else None
        return Ask(price=best, size=size, min_order_size=min_size, tick_size=tick)
