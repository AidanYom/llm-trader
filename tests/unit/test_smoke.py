"""`trader smoke` against a fake broker: what it reports, what it warns about, and that it only reads."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from trader.__main__ import main
from trader.brokers.base import AccountSettings, BrokerError
from trader.brokers.fake import FakeBroker
from trader.models import AccountState, Policy
from trader.settings import load_config
from trader.smoke import SmokeBroker, SmokeReport, run_smoke

NOW = datetime(2026, 9, 28, 13, 5, tzinfo=UTC)  # Monday, 09:05 in New York
SETTINGS = AccountSettings(
    status="ACTIVE",
    trading_blocked=False,
    account_blocked=False,
    trade_suspended_by_user=False,
    suspend_trade=False,
    buying_power=10_000.0,
    no_shorting=True,
    max_margin_multiplier=1.0,
    max_options_trading_level=0,
)


# Two blocked tickers and the patterns that catch them, fixed so tuning policy.yaml never changes these tests.
POLICY = replace(
    load_config(Path(__file__).resolve().parents[2]).policy,
    blocked_symbols=frozenset({"TQQQ", "SOXL"}),
    blocked_name_patterns=("3X", "ProShares UltraPro"),
)
NAMES: dict[str, str | None] = {
    "TQQQ": "ProShares UltraPro QQQ",
    "SOXL": "Direxion Daily Semiconductor Bull 3X Shares",
    "SHV": "iShares Short Treasury Bond ETF",
    "NOSUCHSYM": None,  # the fake makes up a name for any symbol; Alpaca doesn't
}


class SmokeFake(FakeBroker):
    """FakeBroker with the account settings AlpacaBroker reports. Its market is closed at weekends."""

    def __init__(self, settings: AccountSettings = SETTINGS, **kwargs: Any) -> None:
        kwargs.setdefault("bars", {"NOSUCHSYM": []})  # the fake makes up bars for any symbol; Alpaca doesn't
        kwargs.setdefault("asset_names", NAMES)
        super().__init__(now=NOW, **kwargs)
        self.settings = settings

    def account_settings(self) -> AccountSettings:
        self.calls.append("account_settings")
        return self.settings


class FailingAccount(SmokeFake):
    def get_account(self) -> AccountState:
        raise BrokerError("read the account: HTTP 401: unauthorized")


class UnknownCash(SmokeFake):
    def get_account(self) -> AccountState:
        return AccountState(equity=math.nan, cash=math.nan)


def smoke(broker: SmokeBroker | None = None, policy: Policy = POLICY) -> SmokeReport:
    return run_smoke(SmokeFake() if broker is None else broker, now=NOW, feed="sip", policy=policy)


def test_a_healthy_account_passes_with_every_check_ok() -> None:
    report = smoke()

    assert (report.failures, report.warnings) == (0, 0)
    lines = report.text.splitlines()
    assert lines[0] == "trader smoke: Alpaca paper account, sip data, 2026-09-28 09:05 ET"
    assert lines[2:7] == [
        "[ok]   account: equity $10,000.00 · cash $10,000.00 · 0 positions",
        "[ok]   account status: ACTIVE · buying power $10,000.00",
        "[ok]   configuration: no_shorting true · max_margin_multiplier 1 · max_options_trading_level 0",
        "[ok]   calendar: today, 2026-09-28 (Monday), is a trading day",
        "         next sessions: 2026-09-29, 2026-09-30, 2026-10-01",
    ]
    assert "         last completed session: 2026-09-25" in lines
    spy = next(line for line in lines if line.startswith("[ok]   bars: SPY "))
    assert spy.startswith("[ok]   bars: SPY 70 sessions, ")
    assert " to 2026-09-25 · last close " in spy
    assert "[ok]   bars with the unknown ticker NOSUCHSYM: left out, and SPY's bars came back" in lines
    assert "[ok]   market news, last 24 hours: 7 stories shown (untrusted third-party text)" in lines
    assert "[ok]   SPY news, last 72 hours: 2 stories shown (untrusted third-party text)" in lines
    assert lines[-1] == "Smoke passed: 0 failures, 0 warnings."


def test_account_settings_the_app_doesnt_expect_are_warnings() -> None:
    risky = replace(SETTINGS, no_shorting=False, max_margin_multiplier=4.0, max_options_trading_level=2)

    report = smoke(SmokeFake(risky))

    assert (report.failures, report.warnings) == (0, 1)
    lines = report.text.splitlines()
    assert (
        "[warn] configuration: no_shorting false · max_margin_multiplier 4 · max_options_trading_level 2"
        in lines
    )
    assert "         no_shorting is false: turn it on, so the account can never go short" in lines
    assert "         max_margin_multiplier is 4: set it to 1, so buys can't use margin" in lines
    assert "         max_options_trading_level is 2: set it to 0" in lines
    assert lines[-1] == "Smoke passed: 0 failures, 1 warning."


def test_an_unset_options_level_is_fine() -> None:
    report = smoke(SmokeFake(replace(SETTINGS, max_options_trading_level=None)))

    assert report.warnings == 0
    assert "max_options_trading_level unset" in report.text


def test_a_blocked_account_is_a_warning() -> None:
    report = smoke(SmokeFake(replace(SETTINGS, trading_blocked=True)))

    assert (
        "[warn] account status: ACTIVE · trading_blocked · buying power $10,000.00"
        in report.text.splitlines()
    )


def test_unknown_equity_or_cash_is_a_warning() -> None:
    report = smoke(UnknownCash())

    lines = report.text.splitlines()
    assert "[warn] account: equity unknown · cash unknown · 0 positions" in lines
    assert "         equity or cash is unknown, so every buy would be rejected" in lines


def test_a_failed_read_is_a_failure_and_the_other_checks_still_run() -> None:
    report = smoke(FailingAccount())

    lines = report.text.splitlines()
    assert "[FAIL] account: BrokerError: read the account: HTTP 401: unauthorized" in lines
    assert any(line.startswith("[ok]   bars: SPY") for line in lines)
    assert lines[-1] == "Smoke failed: 1 failure, 0 warnings."


def test_bars_that_stop_before_the_last_session_are_a_warning() -> None:
    stale = FakeBroker(now=NOW).completed_sessions("SPY")[:-2]  # ends on Wednesday

    report = smoke(SmokeFake(bars={"SPY": stale, "NOSUCHSYM": []}))

    assert (
        "         the last bar isn't from the last completed session, 2026-09-25" in report.text.splitlines()
    )


def test_bars_with_too_short_a_history_are_a_warning() -> None:
    recent = FakeBroker(now=NOW).completed_sessions("XLK")[-30:]

    report = smoke(SmokeFake(bars={"XLK": recent, "NOSUCHSYM": []}))

    lines = report.text.splitlines()
    assert any(line.startswith("[warn] bars: XLK 30 sessions, ") for line in lines)
    assert "         only 30 of 70 sessions" in lines


def test_bars_coming_back_for_the_unknown_ticker_are_a_warning() -> None:
    report = smoke(SmokeFake(bars={}))  # so the fake makes up bars for it too

    lines = report.text.splitlines()
    assert "[warn] bars with the unknown ticker NOSUCHSYM: bars came back for NOSUCHSYM" in lines


def test_smoke_only_reads() -> None:
    broker = SmokeFake()

    smoke(broker)

    # SmokeBroker has no cancel or submit method, so mypy also rejects any such call in smoke.py.
    assert set(broker.calls) == {
        "get_account",
        "account_settings",
        "is_trading_day",
        "get_daily_bars",
        "get_news",
        "get_asset_names",
    }


def test_the_name_patterns_are_checked_against_real_names() -> None:
    lines = smoke().text.splitlines()

    assert (
        "[ok]   asset names against blocked_name_patterns: "
        "2 blocked tickers, 6 bond funds that must not match" in lines
    )
    assert '         SOXL "Direxion Daily Semiconductor Bull 3X Shares": matches "3X"' in lines
    assert '         TQQQ "ProShares UltraPro QQQ": matches "ProShares UltraPro"' in lines
    assert '         SHV "iShares Short Treasury Bond ETF": no match, as it should be' in lines


def test_a_blocked_ticker_no_pattern_matches_is_a_warning() -> None:
    report = smoke(policy=replace(POLICY, blocked_name_patterns=("3X",)))

    lines = report.text.splitlines()
    assert lines[-1] == "Smoke passed: 0 failures, 1 warning."
    assert (
        '         TQQQ "ProShares UltraPro QQQ": matches no pattern '
        "(fine only if it isn't leveraged or inverse)"
    ) in lines


def test_a_bond_fund_a_pattern_matches_is_a_warning() -> None:
    report = smoke(policy=replace(POLICY, blocked_name_patterns=("3X", "ProShares UltraPro", "Short")))

    assert (
        '         SHV "iShares Short Treasury Bond ETF": matches "Short", but it\'s a cash-like bond fund'
        in (report.text.splitlines())
    )
    assert report.warnings == 1


def test_a_name_for_the_made_up_ticker_is_a_warning() -> None:
    report = smoke(SmokeFake(asset_names={**NAMES, "NOSUCHSYM": "Made-Up Fund"}))

    assert '         NOSUCHSYM "Made-Up Fund": a made-up ticker has a name' in report.text.splitlines()
    assert report.warnings == 1


def test_trader_smoke_exits_0_when_every_read_works(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("trader.__main__._alpaca", lambda secrets: SmokeFake())

    assert main(["smoke"]) == 0
    assert "Smoke passed" in capsys.readouterr().out


def test_trader_smoke_exits_1_when_a_read_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("trader.__main__._alpaca", lambda secrets: FailingAccount())

    assert main(["smoke"]) == 1
    assert "[FAIL] account: BrokerError: read the account: HTTP 401" in capsys.readouterr().out


def test_trader_smoke_without_keys_names_the_missing_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["smoke"]) == 1
    assert capsys.readouterr().err.startswith("trader: ALPACA_API_KEY is not set")
