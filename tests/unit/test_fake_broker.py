from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from trader.brokers.base import Broker, BrokerError, CancelledOrder, SubmittedOrder
from trader.brokers.fake import CANARY_HEADLINE, FakeBroker, Holding, OpenOrder, synthetic_bars
from trader.models import Bar, Order, Side
from trader.settings import load_strategy

MONDAY_PREMARKET = datetime(2026, 9, 28, 12, 31, tzinfo=UTC)  # 08:31 in New York
FRIDAY = date(2026, 9, 25)
SUNDAY = date(2026, 9, 27)
SHIPPED_STRATEGY = Path(__file__).resolve().parents[2] / "config" / "strategy.yaml"

STALE_ENTRY = OpenOrder(broker_order_id="o-1", symbol="IGV", side=Side.BUY)
STOP_LEG = OpenOrder(broker_order_id="o-2", symbol="SMH", side=Side.SELL)
TAKE_PROFIT_LEG = OpenOrder(broker_order_id="o-3", symbol="SMH", side=Side.SELL)
BUY = Order(symbol="URA", side=Side.BUY, qty=10, limit_price=40.4, stop_price=37.0)


def test_fake_broker_has_the_brokers_shape() -> None:
    broker: Broker = FakeBroker(now=MONDAY_PREMARKET)  # mypy checks FakeBroker against the protocol

    assert broker.is_paper


def test_bars_are_completed_weekday_sessions_oldest_first() -> None:
    bars = FakeBroker(now=MONDAY_PREMARKET).get_daily_bars(["XLE"], 70)["XLE"]

    assert len(bars) == 70
    assert bars[-1].day == FRIDAY  # Monday's session hasn't happened yet
    assert all(bar.day.weekday() < 5 for bar in bars)
    assert [bar.day for bar in bars] == sorted({bar.day for bar in bars})


def test_a_dates_bar_never_depends_on_when_it_is_requested() -> None:
    early = FakeBroker(now=MONDAY_PREMARKET).get_daily_bars(["XLE", "SMH"], 70)
    late = FakeBroker(now=MONDAY_PREMARKET + timedelta(days=45)).get_daily_bars(["XLE", "SMH"], 120)

    for symbol in ("XLE", "SMH"):
        later_bars = {bar.day: bar for bar in late[symbol]}
        assert all(later_bars[bar.day] == bar for bar in early[symbol])


def test_each_symbol_has_its_own_bars() -> None:
    bars = FakeBroker(now=MONDAY_PREMARKET).get_daily_bars(["XLE", "XLK"], 5)

    assert [bar.close for bar in bars["XLE"]] != [bar.close for bar in bars["XLK"]]


def test_synthetic_bars_are_sane_and_clear_the_default_floors() -> None:
    strategy = load_strategy(SHIPPED_STRATEGY)
    symbols = {strategy.benchmark, *strategy.sector_etfs, *strategy.industry_etfs, "URA", "XYZ", "NVDA"}

    for symbol in symbols:
        bars = synthetic_bars(symbol, date(2027, 1, 1))
        assert all(
            0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high for bar in bars
        )
        assert all(bar.volume > 0 for bar in bars)
        # Appendix C's min_price and min_avg_dollar_volume, so the offline scenario's buys can pass.
        assert min(bar.close for bar in bars) >= 5, symbol
        for end in range(20, len(bars)):
            assert sum(bar.close * bar.volume for bar in bars[end - 20 : end]) / 20 >= 5_000_000, symbol


def test_holidays_have_no_session() -> None:
    broker = FakeBroker(now=MONDAY_PREMARKET, holidays={FRIDAY})

    assert not broker.is_trading_day(FRIDAY)
    assert broker.get_daily_bars(["XLE"], 1)["XLE"][-1].day == date(2026, 9, 24)


def test_weekends_are_closed_unless_the_market_opens_every_day() -> None:
    assert FakeBroker(now=MONDAY_PREMARKET).is_trading_day(date(2026, 9, 28))
    assert not FakeBroker(now=MONDAY_PREMARKET).is_trading_day(SUNDAY)
    assert FakeBroker(now=MONDAY_PREMARKET, open_every_day=True).is_trading_day(SUNDAY)


def test_replaced_bars_are_used_as_given_but_never_today() -> None:
    thursday = Bar(day=date(2026, 9, 24), open=4.0, high=4.2, low=3.9, close=4.1, volume=1_000)
    today = Bar(day=date(2026, 9, 28), open=4.1, high=4.1, low=4.0, close=4.0, volume=10)
    broker = FakeBroker(now=MONDAY_PREMARKET, bars={"TINY": [thursday, today], "GONE": []})

    assert broker.get_daily_bars(["TINY", "GONE", "XLE"], 70).keys() == {"TINY", "XLE"}
    assert broker.get_daily_bars(["TINY"], 70) == {"TINY": [thursday]}


def test_account_values_holdings_at_the_last_close() -> None:
    broker = FakeBroker(
        now=MONDAY_PREMARKET, cash=5_000.0, holdings=[Holding(symbol="XLE", qty=10, avg_entry_price=50.0)]
    )
    close = broker.last_close("XLE")
    assert close is not None

    account = broker.get_account()

    (position,) = account.positions
    assert (position.symbol, position.qty, position.avg_entry_price, position.current_price) == (
        "XLE",
        10,
        50.0,
        close,
    )
    assert position.market_value == round(10 * close, 2)
    assert position.unrealized_plpc == pytest.approx(close / 50.0 - 1)
    assert (account.cash, account.equity) == (5_000.0, round(5_000.0 + position.market_value, 2))


def test_news_is_filtered_by_time_and_symbol_newest_first() -> None:
    broker = FakeBroker(now=MONDAY_PREMARKET)
    since = MONDAY_PREMARKET - timedelta(hours=24)

    market = broker.get_news(None, since, 50)

    assert len(market) == len(broker.news) - 1  # the 30-hour-old story is left out
    assert [item.created_at for item in market] == sorted((item.created_at for item in market), reverse=True)
    assert [item.headline.lower() for item in broker.get_news(["SMH"], since, 30)] == [
        "chip stocks slip on new export restrictions"
    ] * 2
    assert broker.get_news(None, since, 2) == market[:2]


def test_canned_news_includes_the_prompt_injection_canary() -> None:
    news = FakeBroker(now=MONDAY_PREMARKET).get_news(["XYZ"], MONDAY_PREMARKET - timedelta(days=1), 10)

    assert [item.headline for item in news] == [CANARY_HEADLINE]
    assert news[0].created_at.utcoffset() == timedelta(0)


def test_cancelling_stale_entries_leaves_the_legs() -> None:
    broker = FakeBroker(now=MONDAY_PREMARKET, open_orders=[STALE_ENTRY, STOP_LEG, TAKE_PROFIT_LEG])

    assert broker.cancel_open_buy_orders() == [CancelledOrder(broker_order_id="o-1", symbol="IGV")]
    assert broker.open_orders == [STOP_LEG, TAKE_PROFIT_LEG]
    assert broker.cancel_open_buy_orders() == []


def test_asset_names_are_made_up_unless_a_test_gives_them() -> None:
    broker = FakeBroker(
        now=MONDAY_PREMARKET, asset_names={"TECL": "Direxion Daily Technology Bull 3X Shares", "ZZZZ": None}
    )

    assert broker.get_asset_names(["XLE", "TECL", "ZZZZ"]) == {
        "XLE": "XLE Fake Fund",
        "TECL": "Direxion Daily Technology Bull 3X Shares",
    }


def test_a_cancelled_entry_reports_the_shares_it_had_bought() -> None:
    partial = OpenOrder(broker_order_id="o-1", symbol="IGV", side=Side.BUY, filled_qty=3)
    broker = FakeBroker(now=MONDAY_PREMARKET, open_orders=[partial])

    assert broker.cancel_open_buy_orders() == [
        CancelledOrder(broker_order_id="o-1", symbol="IGV", filled_qty=3.0)
    ]


def test_cancelling_a_symbols_open_orders() -> None:
    broker = FakeBroker(now=MONDAY_PREMARKET, open_orders=[STALE_ENTRY, STOP_LEG, TAKE_PROFIT_LEG])

    assert broker.cancel_open_orders("SMH") == ["o-2", "o-3"]
    assert broker.open_orders == [STALE_ENTRY]
    assert broker.cancelled == ["o-2", "o-3"]


def test_submit_records_the_order_and_refuses_a_repeated_client_order_id() -> None:
    broker = FakeBroker(now=MONDAY_PREMARKET)

    receipt = broker.submit(BUY, "llmt-2026-09-28-URA-buy")

    assert receipt == SubmittedOrder(
        broker_order_id="fake-1", status="accepted", client_order_id="llmt-2026-09-28-URA-buy"
    )
    with pytest.raises(BrokerError, match="llmt-2026-09-28-URA-buy has already been used"):
        broker.submit(BUY, "llmt-2026-09-28-URA-buy")
    assert broker.submitted == [(BUY, "llmt-2026-09-28-URA-buy")]


def test_a_submitted_buy_stays_open_until_cancelled() -> None:
    broker = FakeBroker(now=MONDAY_PREMARKET)
    broker.submit(BUY, "llmt-2026-09-28-URA-buy")
    broker.submit(Order(symbol="SMH", side=Side.SELL, qty=5), "llmt-2026-09-28-SMH-sell")

    assert broker.cancel_open_buy_orders() == [CancelledOrder(broker_order_id="fake-1", symbol="URA")]


def test_broker_calls_are_logged_but_reading_is_paper_is_not_one() -> None:
    broker = FakeBroker(now=MONDAY_PREMARKET)

    assert broker.is_paper
    broker.is_trading_day(FRIDAY)
    broker.get_account()
    broker.get_daily_bars(["SPY"], 5)

    assert broker.calls == ["is_trading_day", "get_account", "get_daily_bars"]


def test_now_needs_a_time_zone() -> None:
    with pytest.raises(ValueError, match="has no time zone"):
        FakeBroker(now=datetime(2026, 9, 28, 8, 31))
