"""Fixtures for every test. The integration tests' database fixtures are in tests/integration/conftest.py."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    """Undo configure_logging(), which the CLI calls, so one test's log setup never leaks into the next."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
