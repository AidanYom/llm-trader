"""Fixtures for every test. The integration tests' database fixtures are in tests/integration/conftest.py."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from trader.logs import SDK_LOGGERS


@pytest.fixture(autouse=True)
def no_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the keys `.env` puts in the dev container, so no test can reach Alpaca or Claude (invariant 7).

    A test that builds a real client by mistake then fails on a missing key instead of calling the API.
    """
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_SSM", raising=False)


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    """Undo configure_logging(), which the CLI calls, so one test's log setup never leaks into the next."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    sdk_levels = {name: logging.getLogger(name).level for name in SDK_LOGGERS}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, sdk_level in sdk_levels.items():
        logging.getLogger(name).setLevel(sdk_level)
