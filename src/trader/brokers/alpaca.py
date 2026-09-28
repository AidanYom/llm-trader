"""AlpacaBroker: the Broker for a real Alpaca account (HANDOFF §8), built on alpaca-py.

This is the only module that imports alpaca-py. It maps Alpaca's responses to the domain types in models.py,
and turns every alpaca-py failure into BrokerError: an API error, a network error, or a response that doesn't
validate.

The mapping functions are pure, so tests run them on alpaca-py model objects built locally. AlpacaBroker
reaches alpaca-py's three clients through the protocols below, which list the methods it calls with
alpaca-py's own signatures: the real clients satisfy them, and tests pass fakes.
"""

from __future__ import annotations

import functools
import logging
import math
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.bars import Bar as AlpacaBar
from alpaca.data.models.bars import BarSet
from alpaca.data.models.news import NewsSet
from alpaca.data.requests import NewsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetClass,
    OrderClass,
    OrderSide,
    OrderStatus,
    PositionSide,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.models import AccountConfiguration, Calendar, TradeAccount
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    OrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from trader.brokers.base import AccountSettings, BrokerError, CancelledOrder, SubmittedOrder
from trader.models import NEW_YORK, AccountState, Bar, NewsItem, Order, Position, Side, new_york_date

log = logging.getLogger(__name__)

# What an alpaca-py client returns in place of its model classes when it's built with raw_data=True.
RawData = dict[str, Any]

BARS_DELAY = timedelta(minutes=20)  # the free data plan serves SIP history only once it's 15 minutes old
FEEDS = {"sip": DataFeed.SIP, "delayed_sip": DataFeed.DELAYED_SIP}  # settings.alpaca_data_feed()'s values
MAX_ERROR_TEXT = 300  # characters of an API error's body kept in BrokerError's message
REQUEST_TIMEOUT = (10.0, 30.0)  # seconds to connect, and to wait for a response, on every Alpaca request
ORDERS_LIMIT = 500  # the most orders one listing returns
CANCEL_POLL_S = 0.5  # HANDOFF §8: how often to check whether a cancel has landed
CANCEL_WAIT_S = 8.0  # and for how long
CLOCK_SKEW = timedelta(seconds=30)  # allowed between this machine's clock and Alpaca's
# A cancel has landed once the order is in one of these (HANDOFF §8).
FINISHED = frozenset(
    {
        OrderStatus.CANCELED,
        OrderStatus.FILLED,
        OrderStatus.EXPIRED,
        OrderStatus.REJECTED,
        OrderStatus.REPLACED,
        OrderStatus.DONE_FOR_DAY,
    }
)


class TradingApi(Protocol):
    """The methods AlpacaBroker calls on alpaca-py's TradingClient."""

    def get_account(self) -> TradeAccount | RawData: ...

    def get_account_configurations(self) -> AccountConfiguration | RawData: ...

    def get_all_positions(self) -> list[AlpacaPosition] | RawData: ...

    def get_calendar(self, filters: GetCalendarRequest | None = None, /) -> list[Calendar] | RawData: ...

    def get_orders(self, filter: GetOrdersRequest | None = None, /) -> list[AlpacaOrder] | RawData: ...

    def get_order_by_id(self, order_id: UUID | str, /) -> AlpacaOrder | RawData: ...

    def get_order_by_client_id(self, client_id: str, /) -> AlpacaOrder | RawData: ...

    def cancel_order_by_id(self, order_id: UUID | str, /) -> None: ...

    def submit_order(self, order_data: OrderRequest, /) -> AlpacaOrder | RawData: ...


class BarsApi(Protocol):
    """The method AlpacaBroker calls on alpaca-py's StockHistoricalDataClient."""

    def get_stock_bars(self, request_params: StockBarsRequest, /) -> BarSet | RawData: ...


class NewsApi(Protocol):
    """The method AlpacaBroker calls on alpaca-py's NewsClient, which it builds to return raw JSON."""

    def get_news(self, request_params: NewsRequest, /) -> NewsSet | RawData: ...


def real_clients(
    api_key: str, secret_key: str, *, paper: bool
) -> tuple[TradingClient, StockHistoricalDataClient, NewsClient]:
    """alpaca-py's trading, bars and news clients, each with REQUEST_TIMEOUT on every request."""
    clients = (
        TradingClient(api_key, secret_key, paper=paper),
        StockHistoricalDataClient(api_key, secret_key),
        NewsClient(api_key, secret_key, raw_data=True),
    )
    for client in clients:
        # alpaca-py sends requests with no timeout, so a stalled connection would hang the run (HANDOFF §8).
        # `_session` is the requests.Session behind each alpaca-py client. It isn't public, so a test pins it.
        session = client._session
        # Replacing the method on this one session object is the point: alpaca-py has no timeout setting.
        session.request = functools.partial(session.request, timeout=REQUEST_TIMEOUT)  # type: ignore[method-assign]
    return clients


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AlpacaBroker:
    """The Broker for an Alpaca account: paper unless ALPACA_PAPER is false.

    Only run.py calls its order methods (CLAUDE.md invariant 2).
    """

    def __init__(
        self,
        *,
        trading: TradingApi,
        bars: BarsApi,
        news: NewsApi,
        paper: bool,
        feed: str = "sip",
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if feed not in FEEDS:
            raise ValueError(f"unsupported data feed {feed!r}")
        self._trading = trading
        self._bars = bars
        self._news = news
        self._paper = paper
        self._feed = FEEDS[feed]
        self._clock = clock  # "now" for the bars' end and a submit's start; "today" for an unfinished session
        self._sleep = sleep  # for waiting on cancels
        self._monotonic = monotonic

    @classmethod
    def connect(cls, *, api_key: str, secret_key: str, paper: bool, feed: str) -> AlpacaBroker:
        """AlpacaBroker on alpaca-py's real clients. Building them makes no network call."""
        trading, bars, news = real_clients(api_key, secret_key, paper=paper)
        return cls(trading=trading, bars=bars, news=news, paper=paper, feed=feed)

    # ---- Broker: reads -------------------------------------------------------------------------------------

    @property
    def is_paper(self) -> bool:
        """The ALPACA_PAPER flag the trading client was built with. Paper keys only work against the paper
        endpoint and live keys against the live one, so the flag can't disagree with the account."""
        return self._paper

    def is_trading_day(self, day: date) -> bool:
        with _broker_errors(f"read the calendar for {day.isoformat()}"):
            sessions = _models(self._trading.get_calendar(GetCalendarRequest(start=day, end=day)), Calendar)
            return any(session.date == day for session in sessions)

    def get_account(self) -> AccountState:
        with _broker_errors("read the account"):
            account = _model(self._trading.get_account(), TradeAccount)
            positions = _models(self._trading.get_all_positions(), AlpacaPosition)
            return account_state(account, positions)

    def get_daily_bars(self, symbols: Sequence[str], sessions: int) -> dict[str, list[Bar]]:
        if not symbols or sessions <= 0:
            return {}
        with _broker_errors(f"read daily bars for {_listed(symbols)}"):
            now = self._clock()
            request = bars_request(symbols, sessions, now=now, feed=self._feed)
            bar_set = _model(self._bars.get_stock_bars(request), BarSet)
            return daily_bars(bar_set, sessions, today=new_york_date(now))

    def get_news(self, symbols: Sequence[str] | None, since: datetime, limit: int) -> list[NewsItem]:
        if limit <= 0 or (symbols is not None and not symbols):
            return []
        about = "market news" if symbols is None else f"news about {_listed(symbols)}"
        with _broker_errors(f"read {about}"):
            items, skipped = news_items(
                _model(self._news.get_news(news_request(symbols, since, limit)), dict)
            )
        if skipped:
            log.warning("skipped malformed news stories", extra={"skipped": skipped, "about": about})
        return items[:limit]

    # ---- Broker: orders ------------------------------------------------------------------------------------

    def cancel_open_buy_orders(self) -> list[CancelledOrder]:
        """Cancel every open buy: earlier runs' entries, whose unfilled legs Alpaca cancels with them.

        It doesn't wait for the cancels to land: the model's turns come before any new entry is sent.
        """
        with _broker_errors("cancel open buy orders"):
            request = GetOrdersRequest(status=QueryOrderStatus.OPEN, side=OrderSide.BUY, limit=ORDERS_LIMIT)
            entries = _models(self._trading.get_orders(request), AlpacaOrder)
            for entry in entries:
                self._cancel(entry)
            return [
                CancelledOrder(
                    broker_order_id=str(entry.id), symbol=entry.symbol or "", filled_qty=_filled(entry)
                )
                for entry in entries
            ]

    def cancel_open_orders(self, symbol: str) -> list[str]:
        """Cancel the symbol's open orders, such as its stop and take-profit, and wait for the cancels."""
        with _broker_errors(f"cancel the open orders for {symbol}"):
            request = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], limit=ORDERS_LIMIT)
            orders = _models(self._trading.get_orders(request), AlpacaOrder)
            for order in orders:
                self._cancel(order)
            self._wait_for_cancels(orders)
            return [str(order.id) for order in orders]

    def submit(self, order: Order, client_order_id: str) -> SubmittedOrder:
        """Send the order. If the call fails but Alpaca has the order anyway, it counts as submitted.

        alpaca-py retries HTTP 429 and 504 responses itself, so a 504 can hide an order Alpaca accepted, and
        the retry is then refused as a duplicate. An order with the same client ID from before this call
        belongs to an earlier run, such as the one a --force rerun replaces, so the failure stands
        (HANDOFF §8).
        """
        with _broker_errors(f"submit {order.side.value} {order.qty} {order.symbol}"):
            request = order_request(order, client_order_id)
            started = self._clock()
            try:
                placed = _model(self._trading.submit_order(request), AlpacaOrder)
            except Exception:
                found = self._placed_since(client_order_id, started)
                if found is None:
                    raise
                log.warning(
                    "the order reached Alpaca although its submit failed",
                    extra={"client_order_id": client_order_id, "broker_order_id": str(found.id)},
                )
                placed = found
            return receipt(placed)

    def _cancel(self, order: AlpacaOrder) -> None:
        """Cancel the order. Alpaca cancels the rest of a bracket or OTO group along with it, so a cancel that
        fails because the order is already over, or being cancelled, isn't an error."""
        try:
            self._trading.cancel_order_by_id(order.id)
        except APIError:
            status = self._order(order.id).status
            if status not in FINISHED and status is not OrderStatus.PENDING_CANCEL:
                raise

    def _wait_for_cancels(self, orders: Sequence[AlpacaOrder]) -> None:
        """Poll until every order is finished (HANDOFF §8). Until its cancel lands, an order holds the shares.

        An order that fills more shares while its cancel is pending has changed the position, so the exit that
        follows would sell the wrong amount: that raises too.
        """
        pending = {order.id: order for order in orders}
        deadline = self._monotonic() + CANCEL_WAIT_S
        while True:
            statuses: dict[UUID, OrderStatus] = {}
            for order_id, listed in pending.items():
                current = self._order(order_id)
                more = _filled(current) - _filled(listed)
                if more > 0:
                    raise Unusable(
                        f"order {order_id} filled {more:g} more shares while its cancel was pending, "
                        "so the position changed and the exit isn't sent"
                    )
                statuses[order_id] = current.status
            pending = {
                order_id: order for order_id, order in pending.items() if statuses[order_id] not in FINISHED
            }
            if not pending:
                return
            if self._monotonic() >= deadline:
                waiting = ", ".join(f"{order_id} ({statuses[order_id].value})" for order_id in pending)
                raise Unusable(f"cancels still pending after {CANCEL_WAIT_S:g} s: {waiting}")
            self._sleep(CANCEL_POLL_S)

    def _placed_since(self, client_order_id: str, started: datetime) -> AlpacaOrder | None:
        """The order with this client ID, if Alpaca created it after `started`, allowing for clock skew."""
        with suppress(Exception):  # no such order, or the lookup failed too: the submit's own error stands
            found = _model(self._trading.get_order_by_client_id(client_order_id), AlpacaOrder)
            if found.created_at >= started - CLOCK_SKEW:
                return found
        return None

    def _order(self, order_id: UUID) -> AlpacaOrder:
        return _model(self._trading.get_order_by_id(order_id), AlpacaOrder)

    # ---- Not part of Broker: for trader smoke --------------------------------------------------------------

    def account_settings(self) -> AccountSettings:
        """The account's status and configuration, which `trader smoke` checks."""
        with _broker_errors("read the account settings"):
            account = _model(self._trading.get_account(), TradeAccount)
            configuration = _model(self._trading.get_account_configurations(), AccountConfiguration)
            return account_settings(account, configuration)


# ---- Mapping: Alpaca's responses to domain types -----------------------------------------------------------


class Unusable(Exception):
    """A response the app can't use, such as a short position. Its message is complete as it stands."""


def account_state(account: TradeAccount, positions: Sequence[AlpacaPosition]) -> AccountState:
    """The account as the risk engine sees it (HANDOFF §8).

    A value Alpaca leaves out is NaN, meaning unknown: the engine then rejects buys under `account:`, and the
    snapshot stores NULL. The app is long-only and trades only US stocks and ETFs, so any other position is
    refused rather than passed on.
    """
    return AccountState(
        equity=_number(account.equity),
        cash=_number(account.cash),
        positions=tuple(_position(position) for position in positions),
    )


def _position(position: AlpacaPosition) -> Position:
    if position.asset_class != AssetClass.US_EQUITY:
        raise Unusable(
            f"the account holds {position.symbol}, a {position.asset_class.value} position; "
            "the app only trades US stocks and ETFs"
        )
    qty = _number(position.qty)
    if position.side != PositionSide.LONG or qty < 0:
        raise Unusable(
            f"the account holds a short position in {position.symbol}; the app is long-only, "
            "so turn on no_shorting in Alpaca's account configuration"
        )
    return Position(
        symbol=position.symbol,
        qty=qty,
        avg_entry_price=_number(position.avg_entry_price),
        current_price=_number(position.current_price),
        market_value=_number(position.market_value),
        unrealized_plpc=_number(position.unrealized_plpc),
    )


def account_settings(account: TradeAccount, configuration: AccountConfiguration) -> AccountSettings:
    """The settings `trader smoke` checks. A block flag Alpaca leaves out counts as not blocked."""
    return AccountSettings(
        status=account.status.value,
        trading_blocked=bool(account.trading_blocked),
        account_blocked=bool(account.account_blocked),
        trade_suspended_by_user=bool(account.trade_suspended_by_user),
        suspend_trade=configuration.suspend_trade,
        buying_power=_number(account.buying_power),
        no_shorting=configuration.no_shorting,
        max_margin_multiplier=_number(configuration.max_margin_multiplier),
        max_options_trading_level=configuration.max_options_trading_level,
    )


def bars_request(symbols: Sequence[str], sessions: int, *, now: datetime, feed: DataFeed) -> StockBarsRequest:
    """Daily bars, adjusted for splits and dividends, up to 20 minutes ago (HANDOFF §8).

    The request starts far enough back to hold `sessions` sessions: five sessions take seven calendar days,
    and ten spare days cover holidays.
    """
    start = new_york_date(now) - timedelta(days=sessions * 7 // 5 + 10)
    return StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=datetime(start.year, start.month, start.day, tzinfo=NEW_YORK),
        end=now - BARS_DELAY,
        adjustment=Adjustment.ALL,
        feed=feed,
    )


def daily_bars(bar_set: BarSet, sessions: int, *, today: date) -> dict[str, list[Bar]]:
    """Each symbol's last `sessions` completed sessions, oldest first.

    A bar is dated by its New York date. One dated today or later is dropped, because its session isn't over.
    A symbol with no completed bars is left out.
    """
    found: dict[str, list[Bar]] = {}
    for symbol, alpaca_bars in bar_set.data.items():
        bars = sorted((_bar(bar) for bar in alpaca_bars), key=lambda bar: bar.day)
        completed = [bar for bar in bars if bar.day < today][-sessions:]
        if completed:
            found[symbol] = completed
    return found


def _bar(bar: AlpacaBar) -> Bar:
    return Bar(
        day=new_york_date(bar.timestamp),
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


def news_request(symbols: Sequence[str] | None, since: datetime, limit: int) -> NewsRequest:
    """Up to `limit` headlines and summaries from `since` on, newest first: about `symbols`, or any news."""
    return NewsRequest(
        symbols=None if symbols is None else ",".join(symbols),
        start=since,
        limit=limit,
        include_content=False,
        sort="desc",
    )


def news_items(raw: Mapping[str, Any]) -> tuple[list[NewsItem], int]:
    """The stories in a raw news response, newest first, and how many were skipped as malformed.

    Each story is mapped on its own, so one odd story can't fail the call. Its text is untrusted third-party
    data (CLAUDE.md invariant 9), passed on as data and never interpreted.
    """
    stories = raw.get("news")
    items: list[NewsItem] = []
    skipped = 0
    for story in stories if isinstance(stories, list) else []:
        item = _news_item(story)
        if item is None:
            skipped += 1
        else:
            items.append(item)
    items.sort(key=lambda item: item.created_at, reverse=True)
    return items, skipped


def _news_item(story: object) -> NewsItem | None:
    """A story, or None without a usable time and headline."""
    if not isinstance(story, Mapping):
        return None
    created_at = _timestamp(story.get("created_at"))
    headline = story.get("headline")
    if created_at is None or not isinstance(headline, str) or not headline.strip():
        return None
    summary = story.get("summary")
    symbols = story.get("symbols")
    return NewsItem(
        created_at=created_at,
        headline=headline,
        summary=summary if isinstance(summary, str) else "",
        symbols=tuple(item for item in symbols if isinstance(item, str)) if isinstance(symbols, list) else (),
    )


def _timestamp(value: object) -> datetime | None:
    """An RFC 3339 time, in UTC. Alpaca's carry a zone; one without is read as UTC."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return (moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)).astimezone(UTC)


def order_request(order: Order, client_order_id: str) -> LimitOrderRequest | MarketOrderRequest:
    """The Alpaca request for an order the risk engine built (HANDOFF §8).

    A buy is a GTC limit order with a stop leg (OTO), plus a take-profit leg when it has one (bracket). GTC
    keeps the legs at the broker after the entry fills, and nothing trades in extended hours. A sell is a full
    exit at market, good for the day.
    """
    if order.side is Side.SELL:
        return MarketOrderRequest(
            symbol=order.symbol,
            qty=order.qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
    if order.limit_price is None or order.stop_price is None:  # Order refuses such a buy; this tells mypy
        raise Unusable(f"a buy of {order.symbol} needs a limit price and a stop")
    take_profit = order.take_profit_price
    return LimitOrderRequest(
        symbol=order.symbol,
        qty=order.qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.GTC,
        limit_price=order.limit_price,
        client_order_id=client_order_id,
        order_class=OrderClass.OTO if take_profit is None else OrderClass.BRACKET,
        stop_loss=StopLossRequest(stop_price=order.stop_price),
        take_profit=None if take_profit is None else TakeProfitRequest(limit_price=take_profit),
    )


def receipt(placed: AlpacaOrder) -> SubmittedOrder:
    """What run.py records about an order Alpaca accepted."""
    return SubmittedOrder(
        broker_order_id=str(placed.id), status=placed.status.value, client_order_id=placed.client_order_id
    )


def _filled(order: AlpacaOrder) -> float:
    """The shares the order has filled so far, or 0 when Alpaca leaves it out."""
    filled = _number(order.filled_qty)
    return 0.0 if math.isnan(filled) else filled


def _number(value: str | float | None) -> float:
    """A number from Alpaca, which sends most of them as strings. NaN when it's missing or not a number."""
    if value is None:
        return math.nan
    try:
        return float(value)
    except ValueError:
        return math.nan


# ---- Errors ------------------------------------------------------------------------------------------------


@contextmanager
def _broker_errors(action: str) -> Iterator[None]:
    """Turn any failure in the block into BrokerError (HANDOFF §8), naming what was being done."""
    try:
        yield
    except BrokerError:
        raise
    except Exception as exc:  # every alpaca-py failure: API, network and validation errors alike
        raise BrokerError(f"{action}: {_describe(exc)}") from exc


def _describe(exc: Exception) -> str:
    """The error on one line. Alpaca's keys travel only in request headers, which no error message repeats."""
    text = " ".join(str(exc).split())
    if isinstance(exc, Unusable):
        return text
    if len(text) > MAX_ERROR_TEXT:
        text = text[: MAX_ERROR_TEXT - 1] + "…"
    if isinstance(exc, APIError):
        status = exc.status_code
        return f"HTTP {status}: {text}" if status else f"APIError: {text}"
    return f"{type(exc).__name__}: {text}"


def _model[M](value: object, kind: type[M]) -> M:
    """An alpaca-py result as the model class it should be. Anything else means a client was built wrong."""
    if not isinstance(value, kind):
        raise Unusable(f"alpaca-py returned {type(value).__name__}, not {kind.__name__}")
    return value


def _models[M](value: object, kind: type[M]) -> list[M]:
    if not isinstance(value, list):
        raise Unusable(f"alpaca-py returned {type(value).__name__}, not a list of {kind.__name__}")
    return [_model(item, kind) for item in value]


def _listed(symbols: Sequence[str]) -> str:
    """The symbols for an error message: the first five, then how many more."""
    shown = ", ".join(symbols[:5])
    return shown if len(symbols) <= 5 else f"{shown} and {len(symbols) - 5} more"
