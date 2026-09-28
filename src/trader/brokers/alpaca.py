"""AlpacaBroker: the Broker for a real Alpaca account (HANDOFF §8), built on alpaca-py.

This is the only module that imports alpaca-py. It maps Alpaca's responses to the domain types in models.py,
and turns every alpaca-py failure into BrokerError: an API error, a network error, or a response that doesn't
validate.

The mapping functions are pure, so tests run them on alpaca-py model objects built locally. AlpacaBroker
reaches alpaca-py's three clients through the protocols below, which list the methods it calls with
alpaca-py's own signatures: the real clients satisfy them, and tests pass fakes.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

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
from alpaca.trading.enums import AssetClass, PositionSide
from alpaca.trading.models import AccountConfiguration, Calendar, TradeAccount
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import GetCalendarRequest

from trader.brokers.base import AccountSettings, BrokerError
from trader.models import NEW_YORK, AccountState, Bar, NewsItem, Position, new_york_date

log = logging.getLogger(__name__)

# What an alpaca-py client returns in place of its model classes when it's built with raw_data=True.
RawData = dict[str, Any]

BARS_DELAY = timedelta(minutes=20)  # the free data plan serves SIP history only once it's 15 minutes old
FEEDS = {"sip": DataFeed.SIP, "delayed_sip": DataFeed.DELAYED_SIP}  # settings.alpaca_data_feed()'s values
MAX_ERROR_TEXT = 300  # characters of an API error's body kept in BrokerError's message


class TradingApi(Protocol):
    """The methods AlpacaBroker calls on alpaca-py's TradingClient."""

    def get_account(self) -> TradeAccount | RawData: ...

    def get_account_configurations(self) -> AccountConfiguration | RawData: ...

    def get_all_positions(self) -> list[AlpacaPosition] | RawData: ...

    def get_calendar(self, filters: GetCalendarRequest | None = None, /) -> list[Calendar] | RawData: ...


class BarsApi(Protocol):
    """The method AlpacaBroker calls on alpaca-py's StockHistoricalDataClient."""

    def get_stock_bars(self, request_params: StockBarsRequest, /) -> BarSet | RawData: ...


class NewsApi(Protocol):
    """The method AlpacaBroker calls on alpaca-py's NewsClient, which it builds to return raw JSON."""

    def get_news(self, request_params: NewsRequest, /) -> NewsSet | RawData: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AlpacaBroker:
    """The Broker for an Alpaca account: paper unless ALPACA_PAPER is false."""

    def __init__(
        self,
        *,
        trading: TradingApi,
        bars: BarsApi,
        news: NewsApi,
        paper: bool,
        feed: str = "sip",
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if feed not in FEEDS:
            raise ValueError(f"unsupported data feed {feed!r}")
        self._trading = trading
        self._bars = bars
        self._news = news
        self._paper = paper
        self._feed = FEEDS[feed]
        self._clock = clock  # "now" for the bars' end, and "today" for dropping an unfinished session

    @classmethod
    def connect(cls, *, api_key: str, secret_key: str, paper: bool, feed: str) -> AlpacaBroker:
        """AlpacaBroker on alpaca-py's real clients. Building them makes no network call."""
        return cls(
            trading=TradingClient(api_key, secret_key, paper=paper),
            bars=StockHistoricalDataClient(api_key, secret_key),
            news=NewsClient(api_key, secret_key, raw_data=True),
            paper=paper,
            feed=feed,
        )

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
