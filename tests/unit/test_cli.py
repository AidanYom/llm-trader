from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from trader.__main__ import main
from trader.brokers.fake import FakeBroker
from trader.models import RunMode, RunStatus
from trader.run import RunResult
from trader.scripted import ScriptedClient

NOW = datetime(2026, 9, 28, 12, 31, tzinfo=UTC)


def test_help_exits_zero_and_names_the_program(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.startswith("usage: trader")


@pytest.mark.parametrize("argv", [[], ["run"], ["run", "--mode", "live"], ["trade"]])
def test_bad_arguments_exit_with_usage(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(argv)

    assert exc_info.value.code == 2
    assert "usage: trader" in capsys.readouterr().err


@pytest.mark.parametrize(("mode", "stored"), [("dry-run", RunMode.DRY_RUN), ("submit", RunMode.SUBMIT)])
def test_real_modes_run_against_alpaca_and_claude(
    mode: str, stored: RunMode, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broker, client = FakeBroker(now=NOW), ScriptedClient([])
    keys: list[str] = []
    calls: list[dict[str, Any]] = []

    def claude(api_key: str) -> ScriptedClient:
        keys.append(api_key)
        return client

    def recorded_run(**kwargs: Any) -> RunResult:
        calls.append(kwargs)
        return RunResult(
            run_id=uuid4(), status=RunStatus.COMPLETED, summary="the summary", briefing="# Briefing"
        )

    monkeypatch.setattr("trader.__main__._alpaca", lambda secrets: broker)
    monkeypatch.setattr("trader.__main__.anthropic_client", claude)
    monkeypatch.setattr("trader.__main__.run_daily", recorded_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("DATABASE_URL", "postgresql://trader:trader@localhost:5432/trader")

    assert main(["run", "--mode", mode, "--force", "--show-briefing"]) == 0

    (call,) = calls
    assert (call["mode"], call["broker"], call["client"], call["force"]) == (stored, broker, client, True)
    assert keys == ["test-key-not-real"]
    assert capsys.readouterr().out == "# Briefing\nthe summary\n"


@pytest.mark.parametrize("mode", ["dry-run", "submit"])
def test_real_modes_need_the_keys_before_anything_runs(
    mode: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "--mode", mode]) == 1
    assert capsys.readouterr().err.startswith("trader: ALPACA_API_KEY is not set")

    monkeypatch.setenv("ALPACA_API_KEY", "key-not-real")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret-not-real")
    assert main(["run", "--mode", mode]) == 1
    assert capsys.readouterr().err.startswith("trader: ANTHROPIC_API_KEY is not set")


def test_user_errors_are_one_line_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ALPACA_PAPER", "maybe")

    assert main(["run", "--mode", "offline"]) == 1
    assert capsys.readouterr().err == "trader: ALPACA_PAPER must be true or false, got 'maybe'\n"
