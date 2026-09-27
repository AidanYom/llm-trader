"""Configuration loading (HANDOFF §12). Config is read once at startup and passed down, never read globally.

M1 loads `config/policy.yaml`. M3 adds `strategy.yaml`, prompt assembly and secrets.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from trader.models import Policy, StopPolicy, finite_float, normalize_symbol


class ConfigError(ValueError):
    """A config file is missing, malformed, or holds a value the app can't use."""


# A number's allowed range: the check, and how an error message describes it.
_Range = tuple[Callable[[float], bool], str]
_ABOVE_0: _Range = (lambda v: v > 0, "above 0")
_AT_LEAST_0: _Range = (lambda v: v >= 0, "of at least 0")
_PERCENT: _Range = (lambda v: 0 < v <= 100, "above 0 and at most 100")
_PERCENT_OR_0: _Range = (lambda v: 0 <= v <= 100, "from 0 to 100")
_STOP_PERCENT: _Range = (lambda v: 0 < v < 100, "above 0 and below 100")


def load_policy(path: Path) -> Policy:
    """Read `policy.yaml` into a Policy.

    Raises ConfigError naming the key when one is unknown, missing or unusable. The checks only catch
    values the code can't work with; whether a limit is wise is Aidan's call.
    """
    top = _Section(_read_yaml(path), path, "", _field_names(Policy))
    stop = top.section("stop", _field_names(StopPolicy))
    if not stop.flag("required"):
        # CLAUDE.md invariant 3; the risk engine requires a stop on every buy regardless.
        raise stop.error("required", "must be true: every buy carries a protective stop")
    stop_policy = StopPolicy(
        required=True,
        min_pct=stop.number("min_pct", _STOP_PERCENT),
        max_pct=stop.number("max_pct", _STOP_PERCENT),
    )
    if stop_policy.min_pct > stop_policy.max_pct:
        raise stop.error("min_pct", f"must not be above stop.max_pct ({stop_policy.max_pct:g})")
    return Policy(
        trading_enabled=top.flag("trading_enabled"),
        allow_live_money=top.flag("allow_live_money"),
        max_position_pct=top.number("max_position_pct", _PERCENT),
        max_open_positions=top.count("max_open_positions"),
        max_new_positions_per_week=top.count("max_new_positions_per_week"),
        min_cash_buffer_pct=top.number("min_cash_buffer_pct", _PERCENT_OR_0),
        min_price=top.number("min_price", _ABOVE_0),
        min_avg_dollar_volume=top.number("min_avg_dollar_volume", _AT_LEAST_0),
        max_pct_of_adv=top.number("max_pct_of_adv", _PERCENT),
        entry_limit_buffer_pct=top.number("entry_limit_buffer_pct", _PERCENT_OR_0),
        stop=stop_policy,
        drawdown_freeze_pct=top.number("drawdown_freeze_pct", _PERCENT),
        drawdown_peak_since=top.optional_date("drawdown_peak_since"),
        blocked_symbols=top.symbols("blocked_symbols"),
    )


def _read_yaml(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: can't read the file: {exc.strerror or exc}") from exc
    try:
        return yaml.safe_load(text)
    # PyYAML raises a plain ValueError for an impossible date such as 2026-13-01.
    except (yaml.YAMLError, ValueError) as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc


def _field_names(cls: type[Any]) -> frozenset[str]:
    return frozenset(field.name for field in dataclasses.fields(cls))


class _Section:
    """One YAML mapping. It must have exactly the expected keys; the accessors check each value."""

    def __init__(self, data: object, path: Path, prefix: str, keys: frozenset[str]) -> None:
        self._path = path
        self._prefix = prefix  # "" at the top level, "stop." inside stop
        if not isinstance(data, dict):
            where = f"{prefix.rstrip('.')}: " if prefix else ""
            raise ConfigError(f"{path}: {where}expected a mapping of keys to values, got {data!r}")
        self._data: dict[Any, Any] = data
        unknown = sorted(str(key) for key in data if key not in keys)
        missing = sorted(key for key in keys if key not in data)
        problems = [
            f"{label} key{'s' if len(names) > 1 else ''} {', '.join(prefix + name for name in names)}"
            for label, names in (("unknown", unknown), ("missing", missing))
            if names
        ]
        if problems:
            raise ConfigError(f"{path}: {'; '.join(problems)}")

    def error(self, key: str, problem: str) -> ConfigError:
        return ConfigError(f"{self._path}: {self._prefix}{key}: {problem}")

    def section(self, key: str, keys: frozenset[str]) -> _Section:
        return _Section(self._data[key], self._path, f"{self._prefix}{key}.", keys)

    def flag(self, key: str) -> bool:
        value = self._data[key]
        if not isinstance(value, bool):
            raise self.error(key, f"must be true or false, got {value!r}")
        return value

    def count(self, key: str) -> int:
        value = self._data[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise self.error(key, f"must be a whole number >= 0, got {value!r}")
        return value

    def number(self, key: str, allowed: _Range) -> float:
        value = self._data[key]
        number = finite_float(value)
        check, description = allowed
        if number is None or not check(number):
            raise self.error(key, f"must be a number {description}, got {value!r}")
        return number

    def optional_date(self, key: str) -> date | None:
        value = self._data[key]
        if value is None:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass
        raise self.error(key, f"must be null or a date such as 2026-10-01, got {value!r}")

    def symbols(self, key: str) -> frozenset[str]:
        value = self._data[key]
        if not isinstance(value, list):
            raise self.error(key, f"must be a list of tickers, got {value!r}")
        symbols: set[str] = set()
        for item in value:
            symbol = normalize_symbol(item)
            if symbol is None:
                hint = ""
                if item is None or isinstance(item, bool):
                    hint = (
                        "; YAML reads unquoted words such as ON, YES, NO and NULL as true, false or null, "
                        "so quote them"
                    )
                raise self.error(key, f"{item!r} is not a ticker{hint}")
            symbols.add(symbol)
        return frozenset(symbols)
