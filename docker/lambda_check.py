"""Checks the Lambda image with no network and no AWS: `make image` and CI pipe it into the image's Python.

It imports the handler that the image's CMD names and invokes it with no secrets. Loading config/ from the
working directory must succeed and the first secret must then be missing, which shows that the dependencies,
the config files and the time-zone data are all in the image (HANDOFF §15).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import psycopg

from trader.lambda_handler import handler
from trader.settings import SecretError

ZoneInfo("America/New_York")  # run dates are New York dates
assert psycopg.pq.__impl__ == "binary", psycopg.pq.__impl__  # psycopg[binary] brings its own libpq
try:
    handler({"mode": "dry_run"}, None)
except SecretError as exc:
    assert str(exc).startswith("ALPACA_API_KEY is not set"), exc
else:
    raise SystemExit("the handler ran without any secrets")
print("Lambda image check passed")
