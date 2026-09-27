from __future__ import annotations

import pytest

from trader.__main__ import main


def test_help_exits_zero_and_names_the_program(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.startswith("usage: trader")
