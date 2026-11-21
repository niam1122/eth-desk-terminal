import json
from decimal import Decimal
from pathlib import Path

from eth_desk.book import ClobBook

FIXTURES = Path(__file__).parent / "fixtures"


class _Level:
    def __init__(self, price, size):
        self.price = price
        self.size = size


class _Book:
    def __init__(self, asks, min_order_size, tick_size):
        self.asks = asks
        self.bids = []
        self.min_order_size = min_order_size
        self.tick_size = tick_size


class _Client:
    def __init__(self, book):
        self.book = book

    def get_order_book(self, token_id):
        assert token_id == "token"
        return self.book


def test_best_ask_is_the_lowest_price_and_sums_that_level():
    recorded = json.loads((FIXTURES / "order_book.json").read_text())
    # Reverse the recorded asks so the first element is not the best price.
    asks = [_Level(level["price"], level["size"]) for level in reversed(recorded["asks"])]
    asks.append(_Level(recorded["asks"][0]["price"], "1.5"))
    client = _Client(_Book(asks, recorded["min_order_size"], recorded["tick_size"]))
    best = ClobBook(client).best_ask("token")
    assert best is not None
    assert best.price == Decimal(recorded["asks"][0]["price"])
    assert best.size == Decimal(recorded["asks"][0]["size"]) + Decimal("1.5")
    assert best.min_order_size == Decimal(recorded["min_order_size"])
    assert best.tick_size == Decimal(recorded["tick_size"])
