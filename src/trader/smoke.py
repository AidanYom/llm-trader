"""`trader smoke` (HANDOFF §8): read-only checks of the Alpaca account and market data.

It's the first thing to run with real keys. It makes each read the daily run makes and reports what came
back. Its broker type has only read methods, so smoke can't cancel or submit an order, and it never touches
the database. A read that fails is a failure. Account settings that differ from what the app expects, and
data that looks incomplete, are warnings.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from trader.briefing import avg_dollar_volume, news_line
from trader.brokers.base import AccountSettings
from trader.models import NEW_YORK, AccountState, Bar, NewsItem, Policy, finite_float, new_york_date
from trader.risk import blocked_pattern

SYMBOLS = ("SPY", "XLK")
SESSIONS = 70  # what the briefing asks for (HANDOFF §6)
UNKNOWN_SYMBOL = "NOSUCHSYM"
MARKET_NEWS = 10  # stories shown
SYMBOL_NEWS = 5
LOOK_AROUND_DAYS = 10  # how far the calendar check looks back and ahead for sessions
NEXT_SESSIONS = 3
MAX_LINE = 150  # characters of a news line shown
# Cash-like bond funds whose names say "Short" or "Ultra": blocked_name_patterns must not match them.
LOOK_ALIKES = ("SHV", "BSV", "JPST", "ICSH", "GSY", "PULS")


class SmokeBroker(Protocol):
    """The reads smoke makes. AlpacaBroker has them; this type has no way to cancel or submit."""

    @property
    def is_paper(self) -> bool: ...

    def is_trading_day(self, day: date) -> bool: ...

    def get_account(self) -> AccountState: ...

    def account_settings(self) -> AccountSettings: ...

    def get_daily_bars(self, symbols: Sequence[str], sessions: int) -> dict[str, list[Bar]]: ...

    def get_news(self, symbols: Sequence[str] | None, since: datetime, limit: int) -> list[NewsItem]: ...

    def get_asset_names(self, symbols: Sequence[str]) -> dict[str, str]: ...


class Outcome(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True, kw_only=True)
class Check:
    outcome: Outcome
    title: str
    lines: tuple[str, ...] = ()  # details, shown indented under the title


@dataclass(frozen=True, slots=True, kw_only=True)
class SmokeReport:
    header: str
    checks: tuple[Check, ...]

    @property
    def failures(self) -> int:
        return sum(1 for check in self.checks if check.outcome is Outcome.FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for check in self.checks if check.outcome is Outcome.WARN)

    @property
    def text(self) -> str:
        lines = [self.header, ""]
        for check in self.checks:
            lines.append(f"[{check.outcome.value}]".ljust(7) + check.title)
            lines += [f"         {line}" for line in check.lines]
        verdict = "Smoke failed" if self.failures else "Smoke passed"
        lines += ["", f"{verdict}: {_count(self.failures, 'failure')}, {_count(self.warnings, 'warning')}."]
        return "\n".join(lines)


def run_smoke(broker: SmokeBroker, *, now: datetime, feed: str, policy: Policy) -> SmokeReport:
    """Every check, in order. A check that raises becomes a failure, and the rest still run."""
    today = new_york_date(now)
    last_session: list[date] = []  # the calendar check finds it; the bars check compares against it
    checks = [
        *_checked("account", lambda: _account(broker)),
        *_checked("account settings", lambda: _settings(broker)),
        *_checked("calendar", lambda: _calendar(broker, today, last_session)),
        *_checked("bars", lambda: _bars(broker, last_session[0] if last_session else None)),
        *_checked(f"bars with the unknown ticker {UNKNOWN_SYMBOL}", lambda: _unknown_symbol(broker)),
        *_checked(
            "market news",
            lambda: _news(broker, None, now=now, window=timedelta(hours=24), limit=MARKET_NEWS),
        ),
        *_checked(
            "SPY news",
            lambda: _news(broker, ["SPY"], now=now, window=timedelta(days=3), limit=SYMBOL_NEWS),
        ),
        *_checked("asset names", lambda: _names(broker, policy)),
    ]
    when = now.astimezone(NEW_YORK).strftime("%Y-%m-%d %H:%M")
    account = "paper" if broker.is_paper else "LIVE"
    return SmokeReport(
        header=f"trader smoke: Alpaca {account} account, {feed} data, {when} ET", checks=tuple(checks)
    )


def _checked(title: str, check: Callable[[], list[Check]]) -> list[Check]:
    try:
        return check()
    except Exception as exc:  # smoke reports every failure and carries on with the next check
        return [Check(outcome=Outcome.FAIL, title=f"{title}: {type(exc).__name__}: {exc}")]


def _account(broker: SmokeBroker) -> list[Check]:
    account = broker.get_account()
    known = finite_float(account.equity) is not None and finite_float(account.cash) is not None
    positions = [
        f"{position.symbol}: {position.qty:g} shares, market value {_usd(position.market_value)}"
        for position in account.positions
    ]
    return [
        Check(
            outcome=Outcome.OK if known else Outcome.WARN,
            title=f"account: equity {_usd(account.equity)} · cash {_usd(account.cash)} · "
            f"{_count(len(account.positions), 'position')}",
            lines=tuple(positions)
            + (() if known else ("equity or cash is unknown, so every buy would be rejected",)),
        )
    ]


def _settings(broker: SmokeBroker) -> list[Check]:
    """The account's status and configuration. Aidan sets them in Alpaca; the app never changes them."""
    settings = broker.account_settings()
    blocks = [
        name
        for name, on in (
            ("trading_blocked", settings.trading_blocked),
            ("account_blocked", settings.account_blocked),
            ("trade_suspended_by_user", settings.trade_suspended_by_user),
            ("suspend_trade", settings.suspend_trade),
        )
        if on
    ]
    status = f"account status: {settings.status}" + "".join(f" · {block}" for block in blocks)
    problems = []
    if not settings.no_shorting:
        problems.append("no_shorting is false: turn it on, so the account can never go short")
    if settings.max_margin_multiplier != 1:
        multiplier = settings.max_margin_multiplier
        problems.append(f"max_margin_multiplier is {multiplier:g}: set it to 1, so buys can't use margin")
    if settings.max_options_trading_level not in (None, 0):
        problems.append(f"max_options_trading_level is {settings.max_options_trading_level}: set it to 0")
    level = "unset" if settings.max_options_trading_level is None else settings.max_options_trading_level
    return [
        Check(
            outcome=Outcome.OK if settings.status == "ACTIVE" and not blocks else Outcome.WARN,
            title=f"{status} · buying power {_usd(settings.buying_power)}",
        ),
        Check(
            outcome=Outcome.WARN if problems else Outcome.OK,
            title=f"configuration: no_shorting {str(settings.no_shorting).lower()} · max_margin_multiplier "
            f"{settings.max_margin_multiplier:g} · max_options_trading_level {level}",
            lines=tuple(problems),
        ),
    ]


def _calendar(broker: SmokeBroker, today: date, last_session: list[date]) -> list[Check]:
    """Today, the next few sessions, and the last completed one, which the bars check compares against."""
    open_today = broker.is_trading_day(today)
    ahead = (today + timedelta(days=days) for days in range(1, LOOK_AROUND_DAYS + 1))
    upcoming: list[date] = []
    for day in ahead:
        if len(upcoming) == NEXT_SESSIONS:
            break
        if broker.is_trading_day(day):
            upcoming.append(day)
    behind = (today - timedelta(days=days) for days in range(1, LOOK_AROUND_DAYS + 1))
    previous = next((day for day in behind if broker.is_trading_day(day)), None)
    if previous is not None:
        last_session.append(previous)
    title = (
        f"calendar: today, {today.isoformat()} ({today:%A}), is {'' if open_today else 'not '}a trading day"
    )
    found = bool(upcoming) and previous is not None
    return [
        Check(
            outcome=Outcome.OK if found else Outcome.WARN,
            title=title,
            lines=(
                f"next sessions: {', '.join(day.isoformat() for day in upcoming) or 'none found'}",
                f"last completed session: {previous.isoformat() if previous else 'none found'}",
            ),
        )
    ]


def _bars(broker: SmokeBroker, last_session: date | None) -> list[Check]:
    found = broker.get_daily_bars(list(SYMBOLS), SESSIONS)
    checks = []
    for symbol in SYMBOLS:
        bars = found.get(symbol, [])
        if not bars:
            checks.append(Check(outcome=Outcome.FAIL, title=f"bars: none for {symbol}"))
            continue
        problems = []
        if len(bars) < SESSIONS:
            problems.append(f"only {len(bars)} of {SESSIONS} sessions")
        if last_session is not None and bars[-1].day != last_session:
            problems.append(f"the last bar isn't from the last completed session, {last_session.isoformat()}")
        adv = avg_dollar_volume(bars)
        checks.append(
            Check(
                outcome=Outcome.WARN if problems else Outcome.OK,
                title=f"bars: {symbol} {len(bars)} sessions, {bars[0].day.isoformat()} to "
                f"{bars[-1].day.isoformat()} · last close {bars[-1].close:.2f} · "
                f"20-day average dollar volume {'n/a' if adv is None else _usd_short(adv)}",
                lines=tuple(problems),
            )
        )
    return checks


def _unknown_symbol(broker: SmokeBroker) -> list[Check]:
    """A made-up ticker must be left out of a bars request, not fail it: the model can propose one."""
    found = broker.get_daily_bars(["SPY", UNKNOWN_SYMBOL], 5)
    problems = []
    if "SPY" not in found:
        problems.append("SPY's bars went missing")
    if UNKNOWN_SYMBOL in found:
        problems.append(f"bars came back for {UNKNOWN_SYMBOL}")
    return [
        Check(
            outcome=Outcome.WARN if problems else Outcome.OK,
            title=f"bars with the unknown ticker {UNKNOWN_SYMBOL}: "
            + ("; ".join(problems) if problems else "left out, and SPY's bars came back"),
        )
    ]


def _news(
    broker: SmokeBroker, symbols: list[str] | None, *, now: datetime, window: timedelta, limit: int
) -> list[Check]:
    stories = broker.get_news(symbols, now - window, limit)
    about = "market news" if symbols is None else f"{', '.join(symbols)} news"
    hours = round(window.total_seconds() / 3600)
    return [
        Check(
            outcome=Outcome.OK if stories else Outcome.WARN,
            title=f"{about}, last {hours} hours: {_count(len(stories), 'story', 'stories')} shown "
            "(untrusted third-party text)",
            lines=tuple(_cut(news_line(story)) for story in stories),
        )
    ]


def _names(broker: SmokeBroker, policy: Policy) -> list[Check]:
    """blocked_name_patterns against Alpaca's real names (HANDOFF §8).

    Every blocked_symbols ticker is a leveraged or inverse fund, so its name should match a pattern. The
    cash-like bond funds say "Short" or "Ultra" and must not match, and a made-up ticker has no name.
    """
    blocked = sorted(policy.blocked_symbols)
    names = broker.get_asset_names([*blocked, *LOOK_ALIKES, UNKNOWN_SYMBOL])
    patterns = policy.blocked_name_patterns
    lines, problems = [], 0
    for symbol in blocked:
        name = names.get(symbol)
        pattern = None if name is None else blocked_pattern(name, patterns)
        if name is None:
            lines.append(f"{symbol}: no name at Alpaca, so only blocked_symbols stops it")
        elif pattern is None:
            problems += 1
            lines.append(
                f'{symbol} "{name}": matches no pattern (fine only if it isn\'t leveraged or inverse)'
            )
        else:
            lines.append(f'{symbol} "{name}": matches "{pattern}"')
    for symbol in LOOK_ALIKES:
        name = names.get(symbol)
        pattern = None if name is None else blocked_pattern(name, patterns)
        if pattern is not None:
            problems += 1
            lines.append(f'{symbol} "{name}": matches "{pattern}", but it\'s a cash-like bond fund')
        else:
            lines.append(f'{symbol} "{name}": no match, as it should be' if name else f"{symbol}: no name")
    if UNKNOWN_SYMBOL in names:
        problems += 1
        lines.append(f'{UNKNOWN_SYMBOL} "{names[UNKNOWN_SYMBOL]}": a made-up ticker has a name')
    return [
        Check(
            outcome=Outcome.WARN if problems else Outcome.OK,
            title=f"asset names against blocked_name_patterns: {len(blocked)} blocked tickers, "
            f"{len(LOOK_ALIKES)} bond funds that must not match",
            lines=tuple(lines),
        )
    ]


def _cut(line: str) -> str:
    return line if len(line) <= MAX_LINE else line[: MAX_LINE - 1] + "…"


def _count(number: int, noun: str, plural: str | None = None) -> str:
    return f"{number} {noun if number == 1 else plural or noun + 's'}"


def _usd(amount: float) -> str:
    number = finite_float(amount)
    return "unknown" if number is None else f"${number:,.2f}"


def _usd_short(amount: float) -> str:
    return f"${amount / 1e9:,.2f}B" if amount >= 1e9 else f"${amount / 1e6:,.1f}M"
