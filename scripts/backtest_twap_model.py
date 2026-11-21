#!/usr/bin/env python3
"""Backtest the TWAP settlement model on resolved ETH 15-minute windows.

Read-only. Fetches Polymarket gamma resolutions for recent
``eth-updown-15m-<unix>`` windows and Binance ETHUSDT 1-second klines, then
scores ``TwapSettlementModel`` and ``ZeroDriftLognormalModel`` at 600, 300,
120, 60, 30, and 10 seconds before the window end.

The price path and the price to beat are both the Binance series (the close
of the 1-second candle opened at t−1 stands in for the Chainlink print at
second t). A constant USDT/USD level therefore drops out of S/O and out of
log returns. The label is the real Chainlink settlement from gamma:
Up when ``finalPrice >= priceToBeat``.

Sigma is an EWMA of 1-minute log returns with a 1-hour half-life (60
observations), fit only on prices at or before the checkpoint.

Usage:
    python scripts/backtest_twap_model.py
    python scripts/backtest_twap_model.py --days 4
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eth_desk.pricing import (  # noqa: E402
    TwapSettlementModel,
    ZeroDriftLognormalModel,
    ewma_sigma_per_sqrt_second,
    settlement_twap,
)
from eth_desk.types import PricingInput  # noqa: E402

CHECKPOINTS = (600, 300, 120, 60, 30, 10)
CALIBRATION_EDGES = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
COARSE_EDGES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
WINDOW = 900
WARMUP = 4 * 3600
USER_AGENT = "eth-desk-backtest/0.1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest the ETH 15-minute TWAP model on real resolutions.")
    parser.add_argument("--days", type=float, default=4.0, help="How many days of completed windows to fetch.")
    args = parser.parse_args(argv)
    if args.days <= 0:
        print("days must be positive", file=sys.stderr)
        return 1

    now = int(time.time())
    last_start = now // WINDOW * WINDOW - WINDOW
    n_windows = max(1, int(args.days * 24 * 3600 / WINDOW))
    first_start = last_start - (n_windows - 1) * WINDOW
    starts = list(range(first_start, last_start + 1, WINDOW))
    price_from = first_start - WARMUP - 180
    price_to = last_start + WINDOW + 5

    print(
        f"fetching {len(starts)} gamma windows and Binance 1s klines "
        f"{_iso(price_from)} .. {_iso(price_to)}",
        file=sys.stderr,
    )
    resolutions, fetch_errors = _fetch_resolutions(starts)
    if fetch_errors > max(5, len(starts) // 20):
        print(f"gamma fetch failed for {fetch_errors} of {len(starts)} windows", file=sys.stderr)
        return 1
    try:
        closes = _fetch_binance_closes(price_from, price_to)
    except RuntimeError as exc:
        print(f"Binance 1s klines were not available: {exc}", file=sys.stderr)
        return 1
    if len(closes) < WARMUP:
        print(f"Binance returned only {len(closes)} seconds; need a multi-hour history", file=sys.stderr)
        return 1

    t_min, filled = _fill_closes(closes)
    rows = _score(resolutions, t_min, filled)
    if not rows:
        print("no windows had both a resolution and a price history", file=sys.stderr)
        return 1
    _report(rows, resolutions, fetch_errors, len(starts))
    return 0


def _fetch_resolutions(starts: list[int]) -> tuple[list[dict], int]:
    errors = 0
    found: list[dict] = []

    def one(start: int) -> dict | None:
        url = f"https://gamma-api.polymarket.com/events?slug=eth-updown-15m-{start}"
        payload = _get_json(url)
        if not payload:
            return None
        event = payload[0]
        metadata = event.get("eventMetadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        open_twap = metadata.get("priceToBeat")
        close_twap = metadata.get("finalPrice")
        if open_twap is None or close_twap is None:
            return None
        open_twap = float(open_twap)
        close_twap = float(close_twap)
        return {
            "start": start,
            "up": 1 if close_twap >= open_twap else 0,
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        for index, result in enumerate(pool.map(_guard(one), starts), start=1):
            if isinstance(result, Exception):
                errors += 1
            elif result is not None:
                found.append(result)
            if index % 80 == 0:
                print(f"  gamma {index}/{len(starts)}", file=sys.stderr)
    found.sort(key=lambda row: row["start"])
    return found, errors


def _fetch_binance_closes(start_s: int, end_s: int) -> dict[int, float]:
    chunks = list(range(start_s, end_s, 1000))
    closes: dict[int, float] = {}

    def one(chunk_start: int) -> list:
        last_error: Exception | None = None
        for host in ("https://data-api.binance.vision", "https://api.binance.com"):
            url = (
                f"{host}/api/v3/klines?symbol=ETHUSDT&interval=1s"
                f"&startTime={chunk_start * 1000}&limit=1000"
            )
            try:
                return _get_json(url)
            except (urllib.error.URLError, RuntimeError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
        raise RuntimeError(f"chunk {chunk_start}: {last_error}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        for index, result in enumerate(pool.map(one, chunks), start=1):
            for kline in result:
                opened = int(kline[0]) // 1000
                if start_s <= opened < end_s:
                    closes[opened] = float(kline[4])
            if index % 40 == 0:
                print(f"  binance {index}/{len(chunks)} chunks, {len(closes)} seconds", file=sys.stderr)
    return closes


def _fill_closes(closes: dict[int, float]) -> tuple[int, list[float]]:
    t_min = min(closes)
    t_max = max(closes)
    filled: list[float] = []
    last: float | None = None
    for second in range(t_min, t_max + 1):
        if second in closes:
            last = closes[second]
        filled.append(float("nan") if last is None else last)
    return t_min, filled


def _price_at(t_min: int, filled: list[float], second: int) -> float:
    """Chainlink-style print at `second`: Binance close of the candle opened at second−1."""

    index = (second - 1) - t_min
    if index < 0 or index >= len(filled):
        raise IndexError(second)
    value = filled[index]
    if value != value or value <= 0:
        raise IndexError(second)
    return value


def _sigma_at(t_min: int, filled: list[float], second: int) -> float:
    # 241 samples, one per minute, from second−4h through `second` inclusive.
    prices = [_price_at(t_min, filled, second - WARMUP + 60 * step) for step in range(241)]
    returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    return ewma_sigma_per_sqrt_second(returns, 60.0, 60.0)


def _score(resolutions: list[dict], t_min: int, filled: list[float]) -> list[dict]:
    twap_model = TwapSettlementModel()
    spot_model = ZeroDriftLognormalModel()
    rows: list[dict] = []
    for window in resolutions:
        start = int(window["start"])
        end = start + WINDOW
        if start - WARMUP <= t_min + 5:
            continue
        try:
            path = {second: _price_at(t_min, filled, second) for second in range(start - 63, start - 1)}
            open_proxy = settlement_twap(path, start)
            if open_proxy is None:
                continue
        except IndexError:
            continue
        for seconds_left in CHECKPOINTS:
            checkpoint = end - seconds_left
            try:
                spot = _price_at(t_min, filled, checkpoint)
                sigma = _sigma_at(t_min, filled, checkpoint)
                observed = []
                for second in range(end - 63, end - 1):
                    if second <= checkpoint:
                        observed.append((second, Decimal(str(_price_at(t_min, filled, second)))))
            except (IndexError, ValueError):
                continue
            shared = dict(
                open_price=Decimal(str(open_proxy)),
                current_price=Decimal(str(spot)),
                seconds_remaining=Decimal(seconds_left),
                volatility_per_sqrt_second=Decimal(str(sigma)),
            )
            twap = twap_model.probability_up(
                PricingInput(
                    **shared,
                    observed_seconds=tuple(observed),
                    as_of_unix=checkpoint,
                    window_end_unix=end,
                )
            )
            spot_probability = spot_model.probability_up(PricingInput(**shared))
            rows.append(
                {
                    "start": start,
                    "seconds_left": seconds_left,
                    "up": window["up"],
                    "twap": float(twap),
                    "spot": float(spot_probability),
                    "sigma": sigma,
                }
            )
    return rows


def _report(rows: list[dict], resolutions: list[dict], fetch_errors: int, requested: int) -> None:
    scored_starts = sorted({row["start"] for row in rows})
    # One label per window, taken from the first checkpoint that window has.
    by_window: dict[int, int] = {}
    for row in rows:
        by_window.setdefault(row["start"], row["up"])
    sigmas = [row["sigma"] for row in rows if row["seconds_left"] == 600] or [row["sigma"] for row in rows]
    print(f"windows scored: {len(by_window)} of {requested} requested, {len(resolutions)} resolved, {fetch_errors} gamma fetch errors")
    print(f"range: {_iso(scored_starts[0])} .. {_iso(scored_starts[-1] + WINDOW)} UTC")
    print(f"actual Up rate: {sum(by_window.values()) / len(by_window):.3f}")
    print("path: Binance ETHUSDT 1s closes (S and the open TWAP both from this series)")
    print("label: gamma finalPrice >= priceToBeat (Chainlink TWAP settlement, tie = Up)")
    print("sigma: EWMA of 1-minute log returns, half-life 60 observations (1 hour), no lookahead")
    print(f"median sigma per sqrt-second: {median(sigmas):.3e}")
    print()
    print("Brier score (mean squared error; 0.25 is a coin flip):")
    print(f"{'seconds_left':>13} {'n':>6} {'twap_ewma':>12} {'spot_ewma':>12}")
    for seconds_left in sorted(CHECKPOINTS, reverse=True):
        group = [row for row in rows if row["seconds_left"] == seconds_left]
        if not group:
            continue
        twap_brier = sum((row["twap"] - row["up"]) ** 2 for row in group) / len(group)
        spot_brier = sum((row["spot"] - row["up"]) ** 2 for row in group) / len(group)
        print(f"{seconds_left:13d} {len(group):6d} {twap_brier:12.6f} {spot_brier:12.6f}")

    pooled = [row for row in rows if row["seconds_left"] in (600, 300, 120)]
    print()
    print("Calibration of twap_ewma, pooled over 600/300/120 seconds left:")
    _print_calibration(pooled, CALIBRATION_EDGES)
    for seconds_left in (600, 300, 120):
        print()
        print(f"Calibration of twap_ewma at {seconds_left}s left:")
        _print_calibration([row for row in rows if row["seconds_left"] == seconds_left], COARSE_EDGES)


def _print_calibration(rows: list[dict], edges: tuple[float, ...]) -> None:
    print(f"{'bin':>16} {'n':>6} {'mean_pred':>10} {'freq_up':>8}")
    for index in range(len(edges) - 1):
        lo, hi = edges[index], edges[index + 1]
        if index == 0:
            chosen = [row for row in rows if lo <= row["twap"] <= hi]
            label = f"[{lo:.1f}, {hi:.1f}]"
        else:
            chosen = [row for row in rows if lo < row["twap"] <= hi]
            label = f"({lo:.1f}, {hi:.1f}]"
        if not chosen:
            print(f"{label:>16} {0:6d} {'—':>10} {'—':>8}")
            continue
        mean_pred = sum(row["twap"] for row in chosen) / len(chosen)
        freq = sum(row["up"] for row in chosen) / len(chosen)
        print(f"{label:>16} {len(chosen):6d} {mean_pred:10.3f} {freq:8.3f}")


def _guard(function):
    def wrapped(arg):
        try:
            return function(arg)
        except Exception as exc:  # noqa: BLE001 — one bad window should not abort the fetch
            return exc

    return wrapped


def _get_json(url: str):
    last_error: Exception | None = None
    for attempt in range(4):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except Exception as exc:  # noqa: BLE001 — retry timeouts and HTTP errors from public APIs
            last_error = exc
            time.sleep(min(8.0, 0.4 * (2**attempt)))
    raise RuntimeError(f"GET failed: {last_error}")


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
