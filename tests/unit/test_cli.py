from __future__ import annotations

import pytest

from trader.__main__ import main


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


@pytest.mark.parametrize("mode", ["dry-run", "submit"])
def test_real_modes_wait_for_m4(mode: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--mode", mode]) == 2
    assert f"--mode {mode} needs the Alpaca adapter and the Claude client (M4)" in capsys.readouterr().err


def test_user_errors_are_one_line_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ALPACA_PAPER", "maybe")

    assert main(["run", "--mode", "offline"]) == 1
    assert capsys.readouterr().err == "trader: ALPACA_PAPER must be true or false, got 'maybe'\n"
