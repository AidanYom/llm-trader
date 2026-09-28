"""The `trader` command (HANDOFF §12): `trader run`, `trader report` and `trader smoke`.

argparse, Python's standard command-line parser, reads the arguments. This module only wires config,
secrets, the database, a broker and a model client together, and prints what comes back; the logic lives
in the modules it calls. Library code logs, and only the CLI prints.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from trader.brokers.alpaca import AlpacaBroker
from trader.db.engine import DatabaseUrlError, make_engine
from trader.db.repo import SchemaVersionError
from trader.logs import configure_logging
from trader.models import RunMode, new_york_date
from trader.offline import offline_broker, offline_client
from trader.report import write_report
from trader.run import ForceRefusedError, LiveMoneyError, run_daily, utc_now
from trader.settings import (
    ConfigError,
    SecretError,
    Secrets,
    alpaca_data_feed,
    alpaca_paper,
    load_config,
)
from trader.smoke import run_smoke

# Errors a user can fix. They're printed as one line; anything else keeps its traceback.
USER_ERRORS = (
    ConfigError,
    SecretError,
    DatabaseUrlError,
    SchemaVersionError,
    LiveMoneyError,
    ForceRefusedError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trader",
        description="Daily pre-market trading agent for one Alpaca account.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    run = commands.add_parser(
        "run",
        help="run the day's pipeline once",
        description="Run the day's pipeline once: briefing, agent, risk engine, orders (HANDOFF §3).",
    )
    run.add_argument(
        "--mode",
        required=True,
        choices=["offline", "dry-run", "submit"],
        help="offline: FakeBroker and a scripted model; dry-run: no orders; submit: real orders",
    )
    run.add_argument(
        "--force", action="store_true", help="first abandon today's stale running submit run (submit mode)"
    )
    run.add_argument("--show-briefing", action="store_true", help="print the briefing before the summary")
    report = commands.add_parser(
        "report",
        help="write the weekly review to reports/",
        description="Write the weekly review of recent runs to reports/week-YYYY-MM-DD.md (HANDOFF §11).",
    )
    report.add_argument(
        "--days", type=_positive, default=7, help="how many days to cover, today included (default 7)"
    )
    report.add_argument("--no-baseline", action="store_true", help="leave out the sector ETF baseline")
    report.add_argument(
        "--offline", action="store_true", help="report on offline runs instead of dry-run and submit runs"
    )
    commands.add_parser(
        "smoke",
        help="read-only checks of the Alpaca account and market data",
        description="Read the Alpaca account, its configuration, the calendar, bars and news, and report "
        "what came back (HANDOFF §8). It never places or cancels an order. Needs the Alpaca keys.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
        if args.command == "report":
            return _report(args)
        if args.command == "smoke":
            return _smoke()
        return _run(args)
    except USER_ERRORS as exc:
        print(f"trader: {exc}", file=sys.stderr)
        return 1


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of days >= 1, got {value!r}")
    return number


def _report(args: argparse.Namespace) -> int:
    config = load_config(Path.cwd())
    paper = alpaca_paper(os.environ)
    now = utc_now()
    # Offline runs' baseline comes from the same fake bars they traded on. The Alpaca adapter, for real runs,
    # arrives in M4; until then their baseline shows as n/a.
    broker = offline_broker(now, paper=paper) if args.offline else None
    engine = make_engine(Secrets(os.environ).get("DATABASE_URL"))
    try:
        path = write_report(
            engine,
            today=new_york_date(now),
            days=args.days,
            paper=paper,
            offline=args.offline,
            basket=config.strategy.baseline_basket,
            broker=broker,
            baseline=not args.no_baseline,
        )
    finally:
        engine.dispose()
    print(f"Wrote {path}")
    return 0


def _smoke() -> int:
    """Exit 1 if any read failed. Warnings, such as account settings to change, leave it at 0."""
    report = run_smoke(_alpaca(Secrets(os.environ)), now=utc_now(), feed=alpaca_data_feed(os.environ))
    print(report.text)
    return 1 if report.failures else 0


def _alpaca(secrets: Secrets) -> AlpacaBroker:
    """The Alpaca account that ALPACA_PAPER names. Building it makes no network call."""
    return AlpacaBroker.connect(
        api_key=secrets.get("ALPACA_API_KEY"),
        secret_key=secrets.get("ALPACA_SECRET_KEY"),
        paper=alpaca_paper(os.environ),
        feed=alpaca_data_feed(os.environ),
    )


def _run(args: argparse.Namespace) -> int:
    mode = RunMode(args.mode.replace("-", "_"))
    if mode is not RunMode.OFFLINE:
        print(
            f"trader: --mode {args.mode} needs the Alpaca adapter and the Claude client (M4)", file=sys.stderr
        )
        return 2
    config = load_config(Path.cwd())
    broker = offline_broker(utc_now(), paper=alpaca_paper(os.environ))
    engine = make_engine(Secrets(os.environ).get("DATABASE_URL"))
    try:
        result = run_daily(
            mode=mode, config=config, engine=engine, broker=broker, client=offline_client(), force=args.force
        )
    finally:
        engine.dispose()
    if args.show_briefing and result.briefing:
        print(result.briefing)
    print(result.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
