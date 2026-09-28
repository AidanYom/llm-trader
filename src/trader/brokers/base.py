"""The broker interface (HANDOFF §8), implemented by FakeBroker and, from M4, the Alpaca adapter.

`Broker` is a `typing.Protocol`: an interface checked by shape, so an implementation needn't inherit from it.
Only run.py sends orders through it (CLAUDE.md invariant 2).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from trader.models import AccountState, Bar, NewsItem, Order


class BrokerError(RuntimeError):
    """A broker call failed, or the broker refused a request, such as an order with a repeated client ID."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelledOrder:
    broker_order_id: str
    symbol: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmittedOrder:
    """The broker's receipt for an order it accepted."""

    broker_order_id: str
    status: str  # the broker's own status for the new order, such as "accepted"
    client_order_id: str


class Broker(Protocol):
    @property
    def is_paper(self) -> bool:
        """True for a paper account. Reading it isn't an account call, so the live-money guard reads it."""

    def is_trading_day(self, day: date) -> bool:
        """Whether the market opens on this America/New_York date."""

    def get_account(self) -> AccountState:
        """Equity, cash and open positions."""

    def get_daily_bars(self, symbols: Sequence[str], sessions: int) -> dict[str, list[Bar]]:
        """Each symbol's last `sessions` completed daily sessions, oldest first.

        A symbol the broker has no bars for is left out. Today's session is never included, even once the
        market has opened: the risk engine and the briefing work from completed sessions only.
        """

    def get_news(self, symbols: Sequence[str] | None, since: datetime, limit: int) -> list[NewsItem]:
        """Up to `limit` stories from `since` on, newest first: about `symbols`, or about anything if None."""

    def cancel_open_buy_orders(self) -> list[CancelledOrder]:
        """Cancel every open BUY order: earlier runs' unfilled entries. An entry's legs go with it."""

    def cancel_open_orders(self, symbol: str) -> list[str]:
        """Cancel every open order for the symbol, and return their IDs once every cancel has landed.

        Raises BrokerError if a cancel hasn't landed within the wait (8 s for Alpaca, HANDOFF §8), because the
        shares stay held for that order until it does.
        """

    def submit(self, order: Order, client_order_id: str) -> SubmittedOrder:
        """Send the order: a limit buy with its protective legs, or a market sell of a whole position.

        Raises BrokerError if the broker refuses it, for example for a client_order_id it has already seen.
        """
