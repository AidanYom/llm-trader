from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest

from trader.briefing import (
    UNTRUSTED_NEWS,
    avg_dollar_volume,
    build_briefing,
    news_line,
    period_return,
    price_history_text,
    symbol_stats,
)
from trader.models import (
    AccountState,
    Bar,
    NewsItem,
    Policy,
    Position,
    Prices,
    RiskContext,
    StopPolicy,
    Strategy,
    SymbolStats,
)

RUN_DATE = date(2026, 9, 28)  # a Monday
LAST_SESSION = date(2026, 9, 25)
NOW = datetime(2026, 9, 28, 12, 31, tzinfo=UTC)  # 08:31 in New York

# HANDOFF Appendix C's numbers, fixed here so tuning config/policy.yaml never changes these tests.
POLICY = Policy(
    trading_enabled=True,
    allow_live_money=False,
    max_position_pct=8.0,
    max_open_positions=6,
    max_new_positions_per_week=4,
    min_cash_buffer_pct=5.0,
    min_price=5.0,
    min_avg_dollar_volume=5_000_000.0,
    max_pct_of_adv=1.0,
    entry_limit_buffer_pct=1.0,
    stop=StopPolicy(required=True, min_pct=3.0, max_pct=15.0),
    drawdown_freeze_pct=15.0,
    drawdown_peak_since=None,
    blocked_symbols=frozenset({"TQQQ"}),
)
STRATEGY = Strategy(
    model="claude-sonnet-5",
    max_tokens=4000,
    max_turns=15,
    max_tool_calls=12,
    benchmark="SPY",
    sector_etfs=("XLK", "XLE"),
    industry_etfs=("SMH",),
    baseline_basket=("XLK", "XLE"),
    news_lookback_hours=24,
    max_news_items=3,
    price=Prices(input=2.0, output=10.0, cache_write=2.5, cache_read=0.2),
    system_frame="config/system_frame.md",
    strategy_prompt="config/strategy.md",
)


def sessions(closes: Sequence[float], volume: float = 1_000_000.0) -> list[Bar]:
    """Bars with these closes on consecutive weekdays, the last on Friday LAST_SESSION."""
    days: list[date] = []
    day = LAST_SESSION
    while len(days) < len(closes):
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return [
        Bar(day=day, open=close, high=close + 1, low=close - 1, close=close, volume=volume)
        for day, close in zip(reversed(days), closes, strict=True)
    ]


def stepped(before: float, after: float) -> list[Bar]:
    """64 sessions: 43 closes at `before`, then 21 at `after`, so 1m and 3m returns are after/before − 1."""
    return sessions([before] * 43 + [after] * 21)


def position(symbol: str, market_value: float) -> Position:
    return Position(
        symbol=symbol,
        qty=10,
        avg_entry_price=market_value / 10,
        current_price=market_value / 10,
        market_value=market_value,
        unrealized_plpc=0.05,
    )


def story(minutes_ago: int, headline: str, summary: str = "Summary.", *symbols: str) -> NewsItem:
    return NewsItem(
        created_at=NOW - timedelta(minutes=minutes_ago), headline=headline, summary=summary, symbols=symbols
    )


def briefing(
    account: AccountState | None = None,
    ctx: RiskContext | None = None,
    bars: dict[str, list[Bar]] | None = None,
    position_news: Sequence[NewsItem] = (),
    market_news: Sequence[NewsItem] = (),
) -> str:
    return build_briefing(
        run_date=RUN_DATE,
        account=AccountState(equity=10_000.0, cash=10_000.0) if account is None else account,
        ctx=RiskContext() if ctx is None else ctx,
        policy=POLICY,
        strategy=STRATEGY,
        bars={} if bars is None else bars,
        position_news=position_news,
        market_news=market_news,
    )


def section(text: str, heading: str) -> list[str]:
    """The lines under a heading, up to the next heading of the same or a higher level."""
    level = heading.split(" ")[0]
    lines = text.splitlines()
    start = lines.index(heading) + 1
    end = next(
        (i for i in range(start, len(lines)) if re.match(rf"#{{1,{len(level)}}} ", lines[i])),
        len(lines),
    )
    return [line for line in lines[start:end] if line]


# ---- Return math and stats --------------------------------------------------------------------------------


def test_returns_count_completed_sessions() -> None:
    bars = sessions([100.0 + i for i in range(64)])  # the last close is 163

    assert period_return(bars, 5) == pytest.approx(163 / 158 - 1)
    assert period_return(bars, 21) == pytest.approx(163 / 142 - 1)
    assert period_return(bars, 63) == pytest.approx(163 / 100 - 1)
    assert period_return(bars[1:], 63) is None  # 63 sessions need 64 closes


def test_average_dollar_volume_and_stats_need_20_sessions() -> None:
    bars = sessions([10.0 + i for i in range(20)], volume=1_000.0)

    assert avg_dollar_volume(bars) == pytest.approx(19.5 * 1_000)  # the mean close is 19.5
    assert symbol_stats({" xle": bars, "XLK": bars[1:]}) == {
        "XLE": SymbolStats(symbol="XLE", as_of=LAST_SESSION, last_close=29.0, avg_dollar_volume_20d=19_500.0)
    }


# ---- Sections ----------------------------------------------------------------------------------------


def test_briefing_sections_come_in_order() -> None:
    headings = [line for line in briefing().splitlines() if line.startswith("#")]

    assert headings == [
        "# Daily briefing: 2026-09-28 (pre-market, US/Eastern)",
        "## Account",
        "## Risk budget (enforced in code)",
        "## Sector and industry strength",
        "## News, last 24h",
        "### About your positions",
        "### Market",
    ]


def test_account_lists_positions_largest_first() -> None:
    account = AccountState(
        equity=10_000.0, cash=8_000.0, positions=(position("XLE", 800.0), position("SMH", 1_200.0))
    )

    lines = section(briefing(account), "## Account")

    assert lines[0] == (
        "Equity $10,000.00 · cash $8,000.00 (80.0% of equity) · invested $2,000.00 (20.0%) · "
        "open positions 2 of 6"
    )
    assert lines[3:] == [
        "| SMH | 10 | $120.00 | $120.00 | +5.0% | 12.0% |",
        "| XLE | 10 | $80.00 | $80.00 | +5.0% | 8.0% |",
    ]


def test_account_line_shows_how_much_is_invested() -> None:
    nearly_full = AccountState(equity=10_000.0, cash=1_200.0, positions=(position("XLE", 8_800.0),))
    unknown = AccountState(
        equity=10_000.0, cash=1_200.0, positions=(position("XLE", 4_400.0), position("SMH", math.nan))
    )

    assert "· invested $8,800.00 (88.0%) ·" in section(briefing(nearly_full), "## Account")[0]
    assert "· invested n/a ·" in section(briefing(unknown), "## Account")[0]


def test_account_without_positions_says_so() -> None:
    assert section(briefing(), "## Account")[1:] == ["No open positions."]


def test_risk_budget_shows_what_the_engine_enforces() -> None:
    account = AccountState(
        equity=10_000.0, cash=6_000.0, positions=(position("XLE", 2_000.0), position("SMH", 2_000.0))
    )

    lines = section(
        briefing(account, RiskContext(new_positions_this_week=1)), "## Risk budget (enforced in code)"
    )

    assert lines == [
        "- New positions left this week: 3 of 4",
        "- Open position slots: 4 of 6",
        "- Cash available for buys: $5,500.00 (cash minus the 5% buffer; sale proceeds don't count)",
        "- Max per position: 8% of equity ($800.00), existing holding included",
        "- Stop: 3–15% below the last close, required on every buy",
        "- Minimum price $5.00; minimum 20-day average dollar volume $5.0M",
        "- Entries: limit at the last close + 1%; unfilled entries are cancelled at the next run",
        "- Drawdown from peak: 0.0% (peak $10,000.00; buys freeze at 15%)",
    ]


def test_cash_available_is_never_negative() -> None:
    lines = section(briefing(AccountState(equity=10_000.0, cash=300.0)), "## Risk budget (enforced in code)")

    assert "- Cash available for buys: $0.00 (cash minus the 5% buffer; sale proceeds don't count)" in lines


@pytest.mark.parametrize(("equity", "frozen"), [(8_400.0, True), (8_500.0, False), (8_600.0, False)])
def test_freeze_banner_shows_exactly_when_buys_are_frozen(equity: float, frozen: bool) -> None:
    text = briefing(AccountState(equity=equity, cash=equity), RiskContext(equity_peak=10_000.0))

    drawdown = (10_000.0 - equity) / 100
    assert f"- Drawdown from peak: {drawdown:.1f}% (peak $10,000.00; buys freeze at 15%)" in text
    banner = (
        f"**FREEZE ACTIVE: equity is {drawdown:.1f}% below its peak, so every buy is rejected. "
        "Sells are still allowed.**"
    )
    assert (banner in text) is frozen


def test_unusable_account_shows_na_instead_of_failing() -> None:
    text = briefing(AccountState(equity=math.nan, cash=math.nan))

    assert "Equity n/a · cash n/a · invested $0.00 · open positions 0 of 6" in text
    assert "- Cash available for buys: n/a (the account can't be sized against, so buys are rejected)" in text
    assert "- Drawdown from peak: n/a" in text


def test_etf_table_is_sorted_by_1m_versus_spy_with_na_last() -> None:
    bars = {
        "SPY": stepped(100.0, 101.0),
        "XLK": stepped(100.0, 102.0),
        "XLE": stepped(100.0, 105.0),
        "SMH": sessions([50.0] * 5),  # too little history for any return
    }

    lines = section(briefing(bars=bars), "## Sector and industry strength")

    assert lines[0] == "SPY: 1w +0.0% · 1m +1.0% · 3m +1.0%"
    assert lines[1:3] == [
        "| ETF | Type | 1w | 1m | 3m | 1m vs SPY | 3m vs SPY |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    assert lines[3:] == [
        "| XLE | sector | +0.0% | +5.0% | +5.0% | +4.0% | +4.0% |",
        "| XLK | sector | +0.0% | +2.0% | +2.0% | +1.0% | +1.0% |",
        "| SMH | industry | n/a | n/a | n/a | n/a | n/a |",
    ]


def test_vs_spy_is_na_without_spy() -> None:
    lines = section(briefing(bars={"XLE": stepped(100.0, 105.0)}), "## Sector and industry strength")

    assert "| XLE | sector | +0.0% | +5.0% | +5.0% | n/a | n/a |" in lines


# ---- News --------------------------------------------------------------------------------------------


def test_news_line_format() -> None:
    item = NewsItem(
        created_at=datetime(2026, 9, 28, 11, 5, tzinfo=UTC),
        headline="  Banks\n rally  ",
        summary="Loan growth\tbeat forecasts.",
        symbols=("KRE", "ZION", "CMA", "KEY", "FITB", "RF"),
    )

    assert (
        news_line(item)
        == "- [Sep 28 07:05 ET] (KRE,ZION,CMA,KEY,FITB) Banks rally: Loan growth beat forecasts."
    )


def test_news_line_cuts_long_text_and_skips_what_is_missing() -> None:
    item = NewsItem(created_at=NOW, headline="H" * 200, summary="", symbols=())

    line = news_line(item)

    assert line == "- [Sep 28 08:31 ET] " + "H" * 159 + "…"
    long_summary = news_line(NewsItem(created_at=NOW, headline="Headline", summary="S" * 300))
    assert long_summary.endswith(": " + "S" * 239 + "…")


def test_news_is_deduped_sorted_newest_first_and_capped() -> None:
    position_news = [
        story(120, "Oil climbs on OPEC+ cuts", "Older copy.", "XLE"),
        story(60, "OIL CLIMBS ON OPEC+ CUTS", "Newer copy.", "XLE"),
    ]
    market_news = [
        story(30, "Oil climbs on OPEC+ cuts", "Also in the market feed.", "XLE"),
        story(300, "Fifth", "", "SPY"),
        story(90, "Second", "", "SPY"),
        story(200, "Fourth", "", "SPY"),
        story(100, "Third", "", "SPY"),
    ]

    text = briefing(position_news=position_news, market_news=market_news)

    news = section(text, "## News, last 24h")
    assert news[0] == UNTRUSTED_NEWS
    assert section(text, "### About your positions") == [
        "- [Sep 28 07:31 ET] (XLE) OIL CLIMBS ON OPEC+ CUTS: Newer copy."
    ]
    # Market leaves out the headline shown under positions, and keeps the newest max_news_items (3).
    assert section(text, "### Market") == [
        "- [Sep 28 07:01 ET] (SPY) Second",
        "- [Sep 28 06:51 ET] (SPY) Third",
        "- [Sep 28 05:11 ET] (SPY) Fourth",
    ]


def test_empty_news_subsections_say_none() -> None:
    text = briefing()

    assert section(text, "### About your positions") == ["- none"]
    assert section(text, "### Market") == ["- none"]


# ---- The get_price_history tool's text -------------------------------------------------------------


def test_price_history_text_summarizes_then_lists_sessions() -> None:
    bars = sessions([10.0 + i for i in range(25)], volume=2_000_000.0)

    lines = price_history_text("XLE", bars, listed=3).splitlines()

    assert lines[0] == (
        "XLE: last close 34.00 on 2026-09-25 · 1w +17.2% · 1m +161.5% · 3m n/a · 20d high 35.00, low 14.00 · "
        "20d avg dollar volume $49.0M"
    )
    assert lines[1:] == [
        "2026-09-23 close 32.00 vol 2000000",
        "2026-09-24 close 33.00 vol 2000000",
        "2026-09-25 close 34.00 vol 2000000",
    ]


def test_price_history_text_with_short_history() -> None:
    text = price_history_text("NEW", sessions([20.0] * 5), listed=30)

    assert "20d high/low n/a · 20d avg dollar volume n/a" in text
    assert len(text.splitlines()) == 6
