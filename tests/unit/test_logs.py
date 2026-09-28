from __future__ import annotations

import json
import logging
import sys
from datetime import date
from uuid import UUID

import pytest

from trader.logs import JsonFormatter, configure_logging


def record(message: str, *args: object, **extra: object) -> logging.LogRecord:
    entry = logging.LogRecord("trader.run", logging.INFO, __file__, 1, message, args, None)
    entry.__dict__.update(extra)
    return entry


def test_a_record_becomes_one_json_object() -> None:
    line = JsonFormatter().format(record("model turn %d ended with %s", 2, "tool_use"))

    entry = json.loads(line)
    assert entry.keys() == {"time", "level", "logger", "message"}
    assert (entry["level"], entry["logger"], entry["message"]) == (
        "INFO",
        "trader.run",
        "model turn 2 ended with tool_use",
    )
    assert entry["time"].endswith("+00:00")
    assert "\n" not in line


def test_extra_fields_become_keys() -> None:
    run_id = UUID("12345678-1234-5678-1234-567812345678")

    entry = json.loads(
        JsonFormatter().format(record("run started", run_id=run_id, run_date=date(2026, 9, 28), turns=3))
    )

    assert (entry["run_id"], entry["run_date"], entry["turns"]) == (str(run_id), "2026-09-28", 3)


def test_exceptions_are_included() -> None:
    try:
        raise RuntimeError("broker timeout")
    except RuntimeError:
        entry = record("run failed")
        entry.exc_info = sys.exc_info()

    logged = json.loads(JsonFormatter().format(entry))

    assert "RuntimeError: broker timeout" in logged["exception"]


def test_configure_logging_writes_json_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("debug")

    logging.getLogger("trader.test").debug("hello %s", "world", extra={"mode": "offline"})

    entry = json.loads(capsys.readouterr().out)
    assert (entry["message"], entry["level"], entry["mode"]) == ("hello world", "DEBUG", "offline")


@pytest.mark.parametrize(("level", "sdk_level"), [("DEBUG", logging.INFO), ("WARNING", logging.WARNING)])
def test_the_sdks_never_log_below_info(
    level: str, sdk_level: int, capsys: pytest.CaptureFixture[str]
) -> None:
    # At DEBUG the Anthropic SDK would log whole request bodies and response headers.
    configure_logging(level)

    for name in ("anthropic", "anthropic._base_client", "httpx2", "httpcore2.http11", "urllib3", "alpaca"):
        assert logging.getLogger(name).getEffectiveLevel() == sdk_level
    logging.getLogger("anthropic._base_client").debug("Request options: %s", {"headers": "..."})
    assert capsys.readouterr().out == ""
    assert logging.getLogger("trader.run").getEffectiveLevel() == getattr(logging, level)


def test_configure_logging_refuses_an_unknown_level() -> None:
    with pytest.raises(
        ValueError, match="LOG_LEVEL must be a logging level such as INFO or DEBUG, got 'loud'"
    ):
        configure_logging("loud")
