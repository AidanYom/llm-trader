"""The broker interface (HANDOFF §8), implemented by AlpacaBroker (alpaca.py) and FakeBroker (fake.py).

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
    # Shares the entry had bought when its cancel landed. Its legs are cancelled with it, so run.py re-places
    # the stop for these shares (HANDOFF §8).
    filled_qty: float = 0.0
    # False if the cancel still hadn't landed when the wait ran out. Until it does, the entry's legs hold the
    # shares, so no stop can be placed for them yet.
    landed: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountSettings:
    """The Alpaca account's own settings, which `trader smoke` checks (HANDOFF §8). Not part of Broker.

    Aidan sets them in Alpaca; the app never changes them.
    """

    status: str  # ACTIVE when the account can trade
    trading_blocked: bool
    account_blocked: bool
    trade_suspended_by_user: bool
    suspend_trade: bool  # the configuration's switch for the same thing
    buying_power: float
    no_shorting: bool
    max_margin_multiplier: float
    max_options_trading_level: int | None  # None when Alpaca leaves it unset


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

    def get_asset_names(self, symbols: Sequence[str]) -> dict[str, str]:
        """Each asset's name as the broker lists it, such as "Energy Select Sector SPDR Fund".

        The risk engine checks the names of proposed buys for leveraged and inverse funds (HANDOFF §7). A
        symbol the broker doesn't know, or lists without a name, is left out.
        """

    def cancel_open_buy_orders(self) -> list[CancelledOrder]:
        """Cancel every open BUY order, earlier runs' unfilled entries, and wait for the cancels to land.

        An entry's legs go with it. Each cancelled entry comes back with the shares it had filled when its
        cancel landed, or with `landed` false if the wait ran out first (8 s for Alpaca, HANDOFF §8). An entry
        that filled completely before its cancel landed keeps its legs, so it isn't returned.
        """

    def cancel_open_orders(self, symbol: str) -> list[str]:
        """Cancel every open order for the symbol, and return their IDs once every cancel has landed.

        Raises BrokerError if a cancel hasn't landed within the wait (8 s for Alpaca, HANDOFF §8), because the
        shares stay held for that order until it does.
        """

    def submit(self, order: Order, client_order_id: str) -> SubmittedOrder:
        """Send the order: a limit buy with its protective legs, a market sell of a whole position, or a
        protective stop on its own, good until cancelled.

        Raises BrokerError if the broker refuses it, for example for a client_order_id it has already seen.
        """
