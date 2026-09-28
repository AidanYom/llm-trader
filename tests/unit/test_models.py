from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from typing import Any

import pytest

from trader.models import (
    Order,
    Side,
    Verdict,
    VerdictStatus,
    finite_float,
    new_york_date,
    normalize_symbol,
    to_cents,
)


def test_new_york_date_is_the_date_in_new_york() -> None:
    assert new_york_date(datetime(2026, 9, 28, 12, 31, tzinfo=UTC)) == date(2026, 9, 28)
    assert new_york_date(datetime(2026, 9, 28, 3, 0, tzinfo=UTC)) == date(2026, 9, 27)  # 23:00 the day before
    with pytest.raises(ValueError, match="has no time zone"):
        new_york_date(datetime(2026, 9, 28, 8, 31))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("XLK", "XLK"), ("  xlk ", "XLK"), ("brk.b", "BRK.B"), ("ABCDEFGHIJ", "ABCDEFGHIJ")],
)
def test_normalize_symbol_strips_and_uppercases(raw: str, expected: str) -> None:
    assert normalize_symbol(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "BRK B",
        "BTC/USD",  # crypto
        "AAPL250117C00150000",  # an option contract
        "ABCDEFGHIJK",  # 11 characters
        ".XLK",
        "ßX",  # uppercases to the ASCII "SSX"
        None,
        42,
    ],
)
def test_normalize_symbol_rejects_anything_but_a_ticker(raw: object) -> None:
    assert normalize_symbol(raw) is None


def test_finite_float_accepts_ints_and_floats() -> None:
    assert finite_float(5) == 5.0
    assert finite_float(-2.5) == -2.5


@pytest.mark.parametrize("value", [None, True, "5", math.nan, math.inf, -math.inf, 10**400])
def test_finite_float_rejects_everything_else(value: object) -> None:
    assert finite_float(value) is None


def test_to_cents_compares_prices_without_float_noise() -> None:
    assert to_cents(124.63) == 12463
    assert to_cents(50.5 + 0.01) == to_cents(50.51) == 5051
    assert to_cents(math.nan) is None
    assert to_cents(1e307) is None  # × 100 overflows


def test_enum_members_are_plain_strings() -> None:
    # They go into the database and JSON as their values, and parse back from them.
    assert isinstance(Side.BUY, str)
    assert str(VerdictStatus.TRIMMED) == "trimmed"
    assert json.dumps([Side.SELL]) == '["sell"]'
    assert Side("buy") is Side.BUY


def buy_order(**changes: Any) -> Order:
    fields: dict[str, Any] = {
        "symbol": "XLK",
        "side": Side.BUY,
        "qty": 10,
        "limit_price": 50.5,
        "stop_price": 47.5,
        "take_profit_price": 55.0,
    }
    return Order(**(fields | changes))


def test_valid_orders_build() -> None:
    assert buy_order().take_profit_price == 55.0
    assert buy_order(take_profit_price=None).take_profit_price is None
    assert buy_order(stop_price=50.49, take_profit_price=50.51).stop_price == 50.49
    assert Order(symbol="XLE", side=Side.SELL, qty=3).limit_price is None


@pytest.mark.parametrize(
    "changes",
    [
        {"stop_price": None},  # every buy carries a protective stop
        {"limit_price": None},
        {"stop_price": 50.5},  # not below the limit
        {"stop_price": 0.0},
        {"stop_price": math.nan},
        {"take_profit_price": 50.5},  # not above the limit
        {"qty": 0},
        {"qty": 1.5},  # whole shares only
        {"qty": True},
        {"symbol": ""},
        {"side": "short"},
    ],
)
def test_invalid_buy_order_raises(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        buy_order(**changes)


def test_sell_order_takes_no_prices() -> None:
    with pytest.raises(ValueError, match="full exit at market"):
        Order(symbol="XLE", side=Side.SELL, qty=3, limit_price=50.0)


def test_verdict_has_an_order_exactly_when_not_rejected() -> None:
    with pytest.raises(ValueError, match="exactly when"):
        Verdict(
            symbol="XLK",
            action=Side.BUY,
            status=VerdictStatus.REJECTED,
            reasons=("price: x",),
            order=buy_order(),
        )
    with pytest.raises(ValueError, match="exactly when"):
        Verdict(symbol="XLK", action=Side.BUY, status=VerdictStatus.APPROVED)


def test_trimmed_or_rejected_verdict_needs_a_reason() -> None:
    with pytest.raises(ValueError, match="needs a reason"):
        Verdict(symbol="XLK", action=Side.BUY, status=VerdictStatus.REJECTED)
    with pytest.raises(ValueError, match="needs a reason"):
        Verdict(symbol="XLK", action=Side.BUY, status=VerdictStatus.TRIMMED, order=buy_order())


def test_rejected_verdict_cannot_open_a_position() -> None:
    with pytest.raises(ValueError, match="can't open"):
        Verdict(
            symbol="XLK",
            action=Side.BUY,
            status=VerdictStatus.REJECTED,
            reasons=("price: x",),
            opens_new_position=True,
        )
