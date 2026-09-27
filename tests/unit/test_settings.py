from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from trader.settings import ConfigError, load_policy

SHIPPED_POLICY = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"


def policy_file(tmp_path: Path, key: str, line: str) -> Path:
    """The shipped policy.yaml with the line for `key` replaced by `line`, keeping its indentation.

    Tests change keys, not values, so they keep passing when Aidan tunes the numbers.
    """
    text = SHIPPED_POLICY.read_text(encoding="utf-8")
    pattern = re.compile(rf"^(\s*){re.escape(key)}:.*$", re.MULTILINE)
    assert pattern.search(text), f"no {key} line in the shipped policy.yaml"
    path = tmp_path / "policy.yaml"
    path.write_text(pattern.sub(lambda match: match.group(1) + line, text, count=1), encoding="utf-8")
    return path


def test_shipped_policy_loads() -> None:
    policy = load_policy(SHIPPED_POLICY)

    assert policy.stop.required
    assert policy.stop.min_pct <= policy.stop.max_pct
    assert policy.blocked_symbols
    assert all(symbol == symbol.upper() for symbol in policy.blocked_symbols)


def test_misspelt_key_is_named_as_unknown_and_missing(tmp_path: Path) -> None:
    path = policy_file(tmp_path, "max_open_positions", "max_open_positons: 6")

    with pytest.raises(ConfigError, match="unknown key max_open_positons; missing key max_open_positions"):
        load_policy(path)


def test_missing_nested_key_is_named(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"missing key stop\.min_pct"):
        load_policy(policy_file(tmp_path, "min_pct", ""))


def test_stop_not_required_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"stop\.required: must be true"):
        load_policy(policy_file(tmp_path, "required", "required: false"))


@pytest.mark.parametrize(
    ("key", "line", "message"),
    [
        ("trading_enabled", 'trading_enabled: "yes"', "trading_enabled: must be true or false, got 'yes'"),
        ("max_open_positions", "max_open_positions: 6.5", "max_open_positions: must be a whole number >= 0"),
        ("min_price", "min_price: -1", "min_price: must be a number above 0, got -1"),
        ("max_position_pct", "max_position_pct: 150", "must be a number above 0 and at most 100, got 150"),
        ("max_pct", "max_pct: .nan", "stop.max_pct: must be a number above 0 and below 100, got nan"),
        ("min_pct", "min_pct: 99.9", "stop.min_pct: must not be above stop.max_pct"),
        ("drawdown_peak_since", "drawdown_peak_since: soon", "drawdown_peak_since: must be null or a date"),
        ("drawdown_peak_since", "drawdown_peak_since: 2026-10-01 09:30:00", "must be null or a date"),
        ("drawdown_peak_since", "drawdown_peak_since: 2026-13-01", "not valid YAML"),
        ("blocked_symbols", "blocked_symbols: TQQQ", "blocked_symbols: must be a list of tickers"),
        ("blocked_symbols", "blocked_symbols: [TQQQ, BRK B]", "blocked_symbols: 'BRK B' is not a ticker"),
        ("blocked_symbols", "blocked_symbols: [TQQQ, ON]", "True is not a ticker; YAML reads unquoted"),
    ],
)
def test_unusable_value_is_named(tmp_path: Path, key: str, line: str, message: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_policy(policy_file(tmp_path, key, line))


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("drawdown_peak_since: 2026-10-01", date(2026, 10, 1)),
        ('drawdown_peak_since: "2026-10-01"', date(2026, 10, 1)),
        ("drawdown_peak_since: null", None),
    ],
)
def test_peak_since_is_a_date_or_null(tmp_path: Path, line: str, expected: date | None) -> None:
    policy = load_policy(policy_file(tmp_path, "drawdown_peak_since", line))

    assert policy.drawdown_peak_since == expected


def test_blocked_symbols_are_normalized(tmp_path: Path) -> None:
    policy = load_policy(policy_file(tmp_path, "blocked_symbols", "blocked_symbols: [tqqq, ' sqqq', 'ON']"))

    assert policy.blocked_symbols == frozenset({"TQQQ", "SQQQ", "ON"})


def test_unreadable_or_empty_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="can't read the file"):
        load_policy(tmp_path / "missing.yaml")

    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="expected a mapping of keys to values, got None"):
        load_policy(empty)
