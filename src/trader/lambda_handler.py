"""The Lambda entry point (HANDOFF §16): production's scheduled daily run.

EventBridge Scheduler invokes `handler` at 08:31 New York time on weekdays, with an empty event. The mode is
the event's "mode", or else the RUN_MODE variable, and must be dry_run or submit. `"force": true` first
abandons the day's stale running submit run, as `trader run --force` does (HANDOFF §9).

Secrets come from SSM, through the *_SSM variables, and each is read once per execution environment: warm
invocations reuse them. A failed run re-raises, which counts in the function's Errors metric and fires its
alarm. Like the CLI, this module only wires things together; the logic lives in the modules it calls.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from trader.agent import anthropic_client
from trader.brokers.alpaca import AlpacaBroker
from trader.db.engine import make_engine
from trader.logs import configure_logging
from trader.models import RunMode
from trader.run import run_daily
from trader.settings import Secrets, alpaca_data_feed, alpaca_paper, load_config

MODES = (RunMode.DRY_RUN, RunMode.SUBMIT)

# One per execution environment. Building it reads nothing, and warm invocations reuse what it resolves.
SECRETS = Secrets(os.environ)


class EventError(ValueError):
    """The invocation's event, or RUN_MODE, doesn't say how to run."""


def handler(event: object, context: object) -> dict[str, str]:
    """Run the day's pipeline once, and return the run's ID, status and one-screen summary."""
    # Each call replaces the last one's log handler, so configuring on every invocation is safe.
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    mode, force = run_options(event, os.environ)
    config = load_config(Path.cwd())  # /var/task in the Lambda image
    # Every secret is read before the run starts, so a missing one stops it before it writes anything.
    broker, client = _alpaca(SECRETS), anthropic_client(SECRETS.get("ANTHROPIC_API_KEY"))
    engine = make_engine(SECRETS.get("DATABASE_URL"))
    try:
        result = run_daily(mode=mode, config=config, engine=engine, broker=broker, client=client, force=force)
    finally:
        engine.dispose()
    # The runtime returns this as JSON, which has no UUIDs or enums.
    return {"run_id": str(result.run_id), "status": result.status.value, "summary": result.summary}


def run_options(event: object, environ: Mapping[str, str]) -> tuple[RunMode, bool]:
    """The run's mode and force flag. When the event's mode is missing, null or empty, RUN_MODE sets it."""
    if event is None:
        event = {}
    if not isinstance(event, Mapping):
        raise EventError(f"the event must be a JSON object, got {type(event).__name__}")
    value, source = event.get("mode"), "the event's mode"
    if value is None or value == "":
        value, source = environ.get("RUN_MODE", "").strip(), "RUN_MODE"
    if value not in MODES:
        raise EventError(f"{source} must be dry_run or submit, got {value!r}")
    force = event.get("force", False)
    if not isinstance(force, bool):
        raise EventError(f"the event's force must be true or false, got {force!r}")
    return RunMode(value), force


def _alpaca(secrets: Secrets) -> AlpacaBroker:
    """The Alpaca account that ALPACA_PAPER names, as in the CLI. Building it makes no network call."""
    return AlpacaBroker.connect(
        api_key=secrets.get("ALPACA_API_KEY"),
        secret_key=secrets.get("ALPACA_SECRET_KEY"),
        paper=alpaca_paper(os.environ),
        feed=alpaca_data_feed(os.environ),
    )
