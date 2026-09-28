"""AlpacaBroker without the network (HANDOFF §14).

The mapping functions run on alpaca-py's own model classes, built from JSON shaped like the API's responses.
The call sequences run against fakes of the alpaca-py clients' public methods, and a test has mypy check that
the real clients fit where the fakes go. Nothing here patches alpaca-py or requests.
"""

from __future__ import annotations

import functools
import json
import logging
import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.bars import BarSet
from alpaca.data.models.news import NewsSet
from alpaca.data.requests import NewsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.models import AccountConfiguration, Calendar, TradeAccount
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.requests import GetCalendarRequest
from pydantic import ValidationError

from trader.brokers.alpaca import (
    REQUEST_TIMEOUT,
    AlpacaBroker,
    BarsApi,
    NewsApi,
    TradingApi,
    account_state,
    real_clients,
)
from trader.brokers.base import AccountSettings, BrokerError
from trader.models import NEW_YORK, Bar, NewsItem, Position

NOW = datetime(2026, 9, 28, 12, 31, tzinfo=UTC)  # Monday, 08:31 in New York
TODAY = date(2026, 9, 28)
SINCE = NOW - timedelta(hours=24)


# ---- alpaca-py objects, built from JSON shaped like the API's ----------------------------------------------


def trade_account(**fields: Any) -> TradeAccount:
    data: dict[str, Any] = {
        "id": "0b7ad2b1-7c73-4d4c-9b0e-6d1f5f0e8a11",
        "account_number": "PA3TEST",
        "status": "ACTIVE",
        "currency": "USD",
        "cash": "7000.5",
        "equity": "10012.25",
        "buying_power": "7000.5",
        "trading_blocked": False,
        "account_blocked": False,
        "trade_suspended_by_user": False,
    }
    return TradeAccount(**(data | fields))


def position(symbol: str = "XLE", **fields: Any) -> AlpacaPosition:
    data: dict[str, Any] = {
        "asset_id": "3b5b8a4e-2f1c-4d6e-9a7b-1c2d3e4f5a6b",
        "symbol": symbol,
        "exchange": "ARCA",
        "asset_class": "us_equity",
        "avg_entry_price": "90.1",
        "qty": "4",
        "side": "long",
        "market_value": "380.4",
        "cost_basis": "360.4",
        "unrealized_plpc": "0.0555",
        "current_price": "95.1",
    }
    return AlpacaPosition(**(data | fields))


def configuration(**fields: Any) -> AccountConfiguration:
    data: dict[str, Any] = {
        "fractional_trading": True,
        "max_margin_multiplier": "1",
        "no_shorting": True,
        "suspend_trade": False,
        "trade_confirm_email": "all",
        "ptp_no_exception_entry": False,
        "max_options_trading_level": 0,
    }
    return AccountConfiguration(**(data | fields))


def session(day: str) -> Calendar:
    return Calendar(date=day, open="09:30", close="16:00")


def raw_bar(day: date, close: float, volume: float = 1_000_000) -> dict[str, Any]:
    """A daily bar as the API sends it: timestamped at midnight in New York, written in UTC."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=NEW_YORK).astimezone(UTC)
    return {
        "t": midnight.isoformat().replace("+00:00", "Z"),
        "o": close - 0.5,
        "h": close + 1,
        "l": close - 1,
        "c": close,
        "v": volume,
        "n": 1_000,
        "vw": close,
    }


def story(created_at: str, headline: str, **fields: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": 1,
        "headline": headline,
        "summary": "A summary.",
        "created_at": created_at,
        "updated_at": created_at,
        "symbols": ["XLE"],
        "author": "Newswire",
        "source": "benzinga",
        "url": "https://example.com/story",
        "content": "",
        "images": [],
    }
    return data | fields


def api_error(status: int, message: str) -> APIError:
    body = json.dumps({"code": 40010001, "message": message})
    # alpaca-py's APIError has no type annotations. It reads the status from the HTTP error's response.
    return APIError(body, SimpleNamespace(response=SimpleNamespace(status_code=status)))  # type: ignore[no-untyped-call]


def validation_error() -> ValidationError:
    try:
        TradeAccount.model_validate({"id": "not-a-uuid"})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


# ---- Fakes of alpaca-py's clients --------------------------------------------------------------------------


class FakeTrading:
    """alpaca-py's TradingClient, as far as AlpacaBroker uses it: canned responses and a log of requests."""

    def __init__(
        self,
        *,
        account: TradeAccount | dict[str, Any] | None = None,
        positions: Sequence[AlpacaPosition] = (),
        sessions: Sequence[Calendar] = (),
        config: AccountConfiguration | None = None,
    ) -> None:
        self.account = trade_account() if account is None else account
        self.positions = list(positions)
        self.sessions = list(sessions)
        self.config = configuration() if config is None else config
        self.failure: Exception | None = None  # every call raises it, when set
        self.requests: list[object] = []

    def _call(self, request: object = None) -> None:
        if request is not None:
            self.requests.append(request)
        if self.failure is not None:
            raise self.failure

    def get_account(self) -> TradeAccount | dict[str, Any]:
        self._call()
        return self.account

    def get_account_configurations(self) -> AccountConfiguration | dict[str, Any]:
        self._call()
        return self.config

    def get_all_positions(self) -> list[AlpacaPosition] | dict[str, Any]:
        self._call()
        return self.positions

    def get_calendar(self, filters: GetCalendarRequest | None = None, /) -> list[Calendar] | dict[str, Any]:
        self._call(filters)
        return self.sessions


class FakeBars:
    """alpaca-py's StockHistoricalDataClient: bars for the requested symbols it has."""

    def __init__(self, bars: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.raw = bars or {}
        self.failure: Exception | None = None
        self.requests: list[StockBarsRequest] = []

    def get_stock_bars(self, request_params: StockBarsRequest, /) -> BarSet | dict[str, Any]:
        self.requests.append(request_params)
        if self.failure is not None:
            raise self.failure
        wanted = request_params.symbol_or_symbols
        return BarSet({symbol: bars for symbol, bars in self.raw.items() if symbol in wanted})


class FakeNews:
    """alpaca-py's NewsClient built with raw_data=True: its canned stories as raw JSON."""

    def __init__(self, stories: Sequence[object] = ()) -> None:
        self.stories = list(stories)
        self.failure: Exception | None = None
        self.requests: list[NewsRequest] = []

    def get_news(self, request_params: NewsRequest, /) -> NewsSet | dict[str, Any]:
        self.requests.append(request_params)
        if self.failure is not None:
            raise self.failure
        return {"news": list(self.stories)}


def broker(
    trading: FakeTrading | None = None,
    bars: FakeBars | None = None,
    news: FakeNews | None = None,
    *,
    feed: str = "sip",
) -> AlpacaBroker:
    return AlpacaBroker(
        trading=FakeTrading() if trading is None else trading,
        bars=FakeBars() if bars is None else bars,
        news=FakeNews() if news is None else news,
        paper=True,
        feed=feed,
        clock=lambda: NOW,
    )


# ---- Construction ------------------------------------------------------------------------------------------


def test_the_real_alpaca_clients_fit_the_adapter() -> None:
    # mypy checks these assignments against the protocols. Building the clients makes no network call.
    trading: TradingApi = TradingClient("key-not-real", "secret-not-real", paper=True)
    bars: BarsApi = StockHistoricalDataClient("key-not-real", "secret-not-real")
    news: NewsApi = NewsClient("key-not-real", "secret-not-real", raw_data=True)

    assert AlpacaBroker(trading=trading, bars=bars, news=news, paper=True).is_paper


@pytest.mark.parametrize("paper", [True, False])
def test_connect_keeps_the_paper_flag_for_the_live_money_guard(paper: bool) -> None:
    assert AlpacaBroker.connect(api_key="key", secret_key="secret", paper=paper, feed="sip").is_paper is paper


def test_a_feed_without_consolidated_volume_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported data feed 'iex'"):
        broker(feed="iex")


def test_every_alpaca_request_gets_a_timeout() -> None:
    # alpaca-py sets none. The timeout rides on each client's requests.Session, which alpaca-py keeps private,
    # so this test notices if an upgrade moves it.
    for client in real_clients("key-not-real", "secret-not-real", paper=True):
        request = client._session.request
        assert isinstance(request, functools.partial)
        assert request.keywords == {"timeout": REQUEST_TIMEOUT}


# ---- Calendar and account ----------------------------------------------------------------------------------


def test_a_day_is_a_trading_day_when_the_calendar_lists_it() -> None:
    trading = FakeTrading(sessions=[session("2026-09-28")])

    assert broker(trading).is_trading_day(TODAY)
    assert trading.requests == [GetCalendarRequest(start=TODAY, end=TODAY)]


def test_a_day_the_calendar_leaves_out_is_not_a_trading_day() -> None:
    assert not broker(FakeTrading(sessions=[])).is_trading_day(date(2026, 9, 27))
    assert not broker(FakeTrading(sessions=[session("2026-09-28")])).is_trading_day(date(2026, 9, 27))


def test_the_accounts_numbers_arrive_as_strings_and_become_floats() -> None:
    account = broker(FakeTrading(positions=[position("XLE")])).get_account()

    assert (account.equity, account.cash) == (10012.25, 7000.5)
    assert account.positions == (
        Position(
            symbol="XLE",
            qty=4.0,
            avg_entry_price=90.1,
            current_price=95.1,
            market_value=380.4,
            unrealized_plpc=0.0555,
        ),
    )


def test_values_alpaca_leaves_out_are_unknown() -> None:
    state = account_state(
        trade_account(cash=None, equity=None),
        [position(market_value=None, current_price=None, unrealized_plpc=None)],
    )

    assert math.isnan(state.equity) and math.isnan(state.cash)
    (held,) = state.positions
    assert math.isnan(held.market_value) and math.isnan(held.current_price)
    assert math.isnan(held.unrealized_plpc)


def test_a_short_position_is_refused() -> None:
    short = position("XLE", side="short", qty="-4")

    with pytest.raises(BrokerError, match="^read the account: the account holds a short position in XLE; "):
        broker(FakeTrading(positions=[short])).get_account()


def test_a_position_that_isnt_a_us_stock_or_etf_is_refused() -> None:
    crypto = position("BTCUSD", asset_class="crypto", exchange="CRYPTO")

    with pytest.raises(
        BrokerError, match="BTCUSD, a crypto position; the app only trades US stocks and ETFs"
    ):
        broker(FakeTrading(positions=[crypto])).get_account()


def test_account_settings_come_from_the_account_and_its_configuration() -> None:
    trading = FakeTrading(
        account=trade_account(trading_blocked=None),
        config=configuration(max_options_trading_level=None),
    )

    assert broker(trading).account_settings() == AccountSettings(
        status="ACTIVE",
        trading_blocked=False,
        account_blocked=False,
        trade_suspended_by_user=False,
        suspend_trade=False,
        buying_power=7000.5,
        no_shorting=True,
        max_margin_multiplier=1.0,
        max_options_trading_level=None,
    )


# ---- Daily bars --------------------------------------------------------------------------------------------


def test_bars_are_requested_adjusted_from_the_feed_until_twenty_minutes_ago() -> None:
    bars = FakeBars()

    broker(bars=bars, feed="delayed_sip").get_daily_bars(["SPY", "XLK"], 70)

    (request,) = bars.requests
    assert request.symbol_or_symbols == ["SPY", "XLK"]
    assert (request.timeframe.amount, request.timeframe.unit) == (1, TimeFrameUnit.Day)
    assert (request.adjustment, request.feed) == (Adjustment.ALL, DataFeed.DELAYED_SIP)
    # alpaca-py stores the times as naive UTC. The start is 108 days back: 70 sessions at 7 days per 5,
    # plus 10 spare days, from midnight in New York.
    assert request.end == datetime(2026, 9, 28, 12, 11)
    assert request.start == datetime(2026, 6, 12, 4, 0)


def test_bars_are_completed_sessions_oldest_first() -> None:
    days = [date(2026, 9, 24), date(2026, 9, 28), date(2026, 9, 23), date(2026, 9, 25)]  # unsorted, and today
    bars = FakeBars({"SPY": [raw_bar(day, 600.0 + i) for i, day in enumerate(days)]})

    result = broker(bars=bars).get_daily_bars(["SPY"], 70)

    assert [bar.day for bar in result["SPY"]] == [date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)]
    assert result["SPY"][-1] == Bar(
        day=date(2026, 9, 25), open=602.5, high=604.0, low=602.0, close=603.0, volume=1e6
    )


def test_only_the_last_sessions_asked_for_are_kept() -> None:
    bars = FakeBars({"SPY": [raw_bar(date(2026, 9, day), 600.0) for day in (21, 22, 23, 24, 25)]})

    result = broker(bars=bars).get_daily_bars(["SPY"], 2)

    assert [bar.day for bar in result["SPY"]] == [date(2026, 9, 24), date(2026, 9, 25)]


def test_a_bar_is_dated_by_its_new_york_date() -> None:
    evening = {
        **raw_bar(date(2026, 9, 25), 600.0),
        "t": "2026-09-26T02:00:00Z",
    }  # 22:00 on the 25th in New York

    result = broker(bars=FakeBars({"SPY": [evening]})).get_daily_bars(["SPY"], 5)

    assert result["SPY"][0].day == date(2026, 9, 25)


def test_a_symbol_without_completed_bars_is_left_out() -> None:
    bars = FakeBars({"SPY": [raw_bar(date(2026, 9, 25), 600.0)], "XLK": [raw_bar(TODAY, 250.0)]})

    assert list(broker(bars=bars).get_daily_bars(["SPY", "XLK", "NOSUCHSYM"], 70)) == ["SPY"]


def test_no_symbols_or_no_sessions_need_no_request() -> None:
    bars = FakeBars()

    assert broker(bars=bars).get_daily_bars([], 70) == {}
    assert broker(bars=bars).get_daily_bars(["SPY"], 0) == {}
    assert bars.requests == []


# ---- News --------------------------------------------------------------------------------------------------


def test_news_is_requested_as_headlines_about_the_symbols() -> None:
    news = FakeNews()

    broker(news=news).get_news(["XLE", "URA"], SINCE, 30)

    (request,) = news.requests
    assert (request.symbols, request.start, request.limit) == ("XLE,URA", SINCE, 30)
    assert (request.include_content, request.sort) == (False, "desc")


def test_market_news_is_requested_about_no_symbols() -> None:
    news = FakeNews()

    broker(news=news).get_news(None, SINCE, 50)

    assert news.requests[0].symbols is None


def test_news_is_newest_first_in_utc() -> None:
    news = FakeNews(
        [
            story("2026-09-27T14:05:00Z", "Older"),
            story("2026-09-28T07:30:00.123456-04:00", "Newer", symbols=["SPY", "QQQ"]),
        ]
    )

    assert broker(news=news).get_news(None, SINCE, 10) == [
        NewsItem(
            created_at=datetime(2026, 9, 28, 11, 30, 0, 123456, tzinfo=UTC),
            headline="Newer",
            summary="A summary.",
            symbols=("SPY", "QQQ"),
        ),
        NewsItem(
            created_at=datetime(2026, 9, 27, 14, 5, tzinfo=UTC),
            headline="Older",
            summary="A summary.",
            symbols=("XLE",),
        ),
    ]


def test_news_stops_at_the_limit() -> None:
    news = FakeNews([story(f"2026-09-28T0{hour}:00:00Z", f"Story {hour}") for hour in (1, 2, 3)])

    assert [item.headline for item in broker(news=news).get_news(None, SINCE, 2)] == ["Story 3", "Story 2"]


def test_a_malformed_story_is_skipped_not_fatal(caplog: pytest.LogCaptureFixture) -> None:
    news = FakeNews(
        [
            story("2026-09-28T11:00:00Z", "Fine"),
            story("not a time", "Bad time"),
            story("2026-09-28T10:00:00Z", "   "),
            "not a story",
            story("2026-09-28T09:00:00Z", "Bare", summary=None, symbols=None),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="trader.brokers.alpaca"):
        items = broker(news=news).get_news(None, SINCE, 10)

    assert [(item.headline, item.summary, item.symbols) for item in items] == [
        ("Fine", "A summary.", ("XLE",)),
        ("Bare", "", ()),
    ]
    (record,) = caplog.records
    assert record.getMessage() == "skipped malformed news stories"
    assert record.__dict__["skipped"] == 3


def test_news_about_no_symbols_needs_no_request() -> None:
    news = FakeNews([story("2026-09-28T11:00:00Z", "Fine")])

    assert broker(news=news).get_news([], SINCE, 10) == []
    assert broker(news=news).get_news(None, SINCE, 0) == []
    assert news.requests == []


# ---- Errors ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (api_error(401, "unauthorized."), 'HTTP 401: {"code": 40010001, "message": "unauthorized."}'),
        (ConnectionError("Connection reset by peer"), "ConnectionError: Connection reset by peer"),
        (validation_error(), "ValidationError: "),
    ],
)
def test_every_alpaca_failure_becomes_a_broker_error(failure: Exception, message: str) -> None:
    trading, bars, news = FakeTrading(), FakeBars(), FakeNews()
    trading.failure = bars.failure = news.failure = failure
    adapter = broker(trading, bars, news)

    for call, action in [
        (adapter.get_account, "read the account"),
        (lambda: adapter.is_trading_day(TODAY), "read the calendar for 2026-09-28"),
        (lambda: adapter.get_daily_bars(["SPY"], 70), "read daily bars for SPY"),
        (lambda: adapter.get_news(["SPY"], SINCE, 10), "read news about SPY"),
        (adapter.account_settings, "read the account settings"),
    ]:
        with pytest.raises(BrokerError) as exc_info:
            call()
        assert str(exc_info.value).startswith(f"{action}: {message}")
        assert exc_info.value.__cause__ is failure


def test_a_long_error_body_is_cut_short() -> None:
    trading = FakeTrading()
    trading.failure = api_error(502, "x" * 1_000)

    with pytest.raises(BrokerError) as exc_info:
        broker(trading).get_account()

    assert len(str(exc_info.value)) < 350
    assert str(exc_info.value).endswith("…")


def test_raw_json_where_a_model_belongs_is_an_error() -> None:
    with pytest.raises(BrokerError, match="^read the account: alpaca-py returned dict, not TradeAccount$"):
        broker(FakeTrading(account={"cash": "1"})).get_account()


def test_a_long_symbol_list_is_shortened_in_errors() -> None:
    bars = FakeBars()
    bars.failure = ConnectionError("down")
    symbols = ["SPY", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY"]

    with pytest.raises(BrokerError, match="^read daily bars for SPY, XLK, XLF, XLE, XLV and 2 more: "):
        broker(bars=bars).get_daily_bars(symbols, 70)
