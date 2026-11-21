from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from time import sleep as default_sleep

from eth_desk.book import ClobBook
from eth_desk.clock import utcnow
from eth_desk.config import Settings
from eth_desk.engine import FreezeHalt, Services, run
from eth_desk.gamma import GammaClient
from eth_desk.geoblock import GeoblockClient, GeoblockRefusal
from eth_desk.journal import Journal
from eth_desk.live import LiveGateway, live_armed, missing_live_credentials
from eth_desk.prices import ChainlinkFeed
from eth_desk.pricing import ZeroDriftLognormalModel
from eth_desk.redact import secrets_from_env
from eth_desk.state import StateError, load_or_fresh


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="eth-desk",
        description="Paper terminal for Polymarket 15-minute Ethereum Up or Down markets.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Arm live mode. Also requires POLYMARKET_LIVE=1. Off by default. v1 does not send orders.",
    )
    parser.add_argument("--once", action="store_true", help="Run a single loop and exit.")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between loops (default 5).")
    parser.add_argument("--log", default="logs/eth-desk.jsonl", help="JSONL log path.")
    parser.add_argument("--state", default="state/desk.json", help="Persisted bankroll and daily stop.")
    parser.add_argument("--freeze", default="FREEZE", help="Path of the FREEZE file. Exists means stop.")
    parser.add_argument("--heartbeat", type=float, default=60.0, help="Seconds between heartbeat lines.")
    return parser.parse_args(argv)


def default_services(mode: str) -> Services:
    return Services(
        geoblock=GeoblockClient(),
        gamma=GammaClient(),
        book=ClobBook(),
        feed=ChainlinkFeed(),
        model=ZeroDriftLognormalModel(),
        gateway=LiveGateway(armed=(mode == "live")),
    )


def main(
    argv: list[str] | None = None,
    *,
    services: Services | None = None,
    env: dict | None = None,
    clock=None,
    sleep=None,
    stdout=None,
    stderr=None,
) -> int:
    args = parse_args(argv)
    environment = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    now_fn = utcnow if clock is None else clock
    if args.interval <= 0 or args.heartbeat <= 0:
        print("Refusing to start: --interval and --heartbeat must be positive.", file=err)
        return 1
    if args.live and not live_armed(True, environment):
        print(
            "Refusing to start: --live was passed but POLYMARKET_LIVE is not set to 1. "
            "Live trading was not enabled.",
            file=err,
        )
        return 4
    if live_armed(args.live, environment):
        missing = missing_live_credentials(environment)
        if missing:
            print(
                "Refusing to start: live mode is missing " + ", ".join(missing) + ".",
                file=err,
            )
            return 4
        mode = "live"
    else:
        mode = "paper"
        if environment.get("POLYMARKET_LIVE") == "1":
            print(
                "POLYMARKET_LIVE=1 was ignored because --live was not passed. Running in paper mode.",
                file=out,
            )

    settings = Settings(
        mode=mode,
        once=bool(args.once),
        interval_seconds=float(args.interval),
        log_path=Path(args.log),
        freeze_path=Path(args.freeze),
        state_path=Path(args.state),
        heartbeat_seconds=float(args.heartbeat),
    )
    journal = Journal(
        settings.log_path,
        secrets_from_env(environment),
        echo=lambda line: print(line, file=out),
    )
    try:
        state = load_or_fresh(settings.state_path, now_fn())
    except StateError as exc:
        print(f"Refusing to start: {exc}", file=err)
        return 1
    active = services if services is not None else default_services(mode)
    try:
        run(
            settings,
            active,
            state,
            journal,
            now_fn,
            default_sleep if sleep is None else sleep,
        )
    except GeoblockRefusal as exc:
        print(exc.message, file=err)
        return 2
    except FreezeHalt as exc:
        print(exc.message, file=err)
        try:
            journal.daily_summary(state.summary())
            state.save(settings.state_path)
        except Exception:
            pass
        return 3
    except KeyboardInterrupt:
        journal.daily_summary(state.summary())
        state.save(settings.state_path)
        return 0
    except Exception as exc:
        print(f"Stopped on error: {type(exc).__name__}: {exc}", file=err)
        try:
            state.save(settings.state_path)
        except Exception:
            pass
        return 1
    state.save(settings.state_path)
    return 0
