"""Logging (HANDOFF §9): standard-library logging to stdout, one JSON object per line.

The CLI, and from M5 the Lambda handler, calls `configure_logging()` once. Library modules only use
`logging.getLogger(__name__)`, and the `extra` fields of a log call become keys of its JSON object. Nothing
may log a secret (CLAUDE.md invariant 8).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

# Attributes every LogRecord has; anything else on a record came from `extra`.
_STANDARD = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update((key, value) for key, value in vars(record).items() if key not in _STANDARD)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Send every log record to stdout as JSON, at `level` (LOG_LEVEL) and above."""
    name = level.strip().upper() or "INFO"
    if name not in logging.getLevelNamesMapping():
        raise ValueError(f"LOG_LEVEL must be a logging level such as INFO or DEBUG, got {level!r}")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(name)
