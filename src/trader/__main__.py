"""The `trader` command (HANDOFF §12): `trader run` and `trader report`, and `trader smoke` from M4.

argparse, Python's standard command-line parser, reads the arguments. This module only wires config,
secrets, the database, a broker and a model client together, and prints what comes back; the logic lives
in the modules it calls. Library code logs, and only the CLI prints.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from trader.db.engine import DatabaseUrlError, make_engine
from trader.db.repo import SchemaVersionError
from trader.logs import configure_logging
from trader.models import RunMode
from trader.offline import offline_broker, offline_client
from trader.run import ForceRefusedError, LiveMoneyError, run_daily, utc_now
from trader.settings import ConfigError, SecretError, Secrets, alpaca_paper, load_config

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
        return _run(args)
    except USER_ERRORS as exc:
        print(f"trader: {exc}", file=sys.stderr)
        return 1


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
