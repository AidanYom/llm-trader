"""The weekly report renders from real runs (HANDOFF §11 and §14), here the offline scenario's."""

from __future__ import annotations

import os
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine

from trader.__main__ import main
from trader.models import RunMode
from trader.offline import offline_broker, offline_client
from trader.report import baseline_return, write_report
from trader.run import run_daily, utc_now
from trader.settings import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_config(REPO_ROOT)
MONDAY = datetime(2026, 9, 28, 12, 31, tzinfo=UTC)  # 08:31 in New York
TUESDAY = datetime(2026, 9, 29, 12, 31, tzinfo=UTC)


def offline_run(engine: Engine, now: datetime) -> None:
    run_daily(
        mode=RunMode.OFFLINE,
        config=CONFIG,
        engine=engine,
        broker=offline_broker(now),
        client=offline_client(),
        clock=lambda: now,
    )


def test_the_report_renders(engine: Engine, conn: Connection, tmp_path: Path) -> None:
    offline_run(engine, MONDAY)
    offline_run(engine, TUESDAY)
    basket = CONFIG.strategy.baseline_basket

    path = write_report(
        engine,
        today=date(2026, 9, 29),
        days=7,
        paper=True,
        offline=True,
        basket=basket,
        broker=offline_broker(TUESDAY),
        directory=tmp_path,
    )

    assert path == tmp_path / "week-2026-09-29-offline.md"
    text = path.read_text(encoding="utf-8")
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert headings == [
        "## Scorecard",
        "## Behavior",
        "## Current positions (from the 2026-09-29 snapshot)",
        "## Daily log",
    ]
    assert text.startswith("# Weekly review: 2026-09-23 to 2026-09-29 (offline runs)\n")
    assert "| offline | 0 | 2 | 0 | 0 | 0 |" in text
    assert "- Proposals: 4 approved, 0 trimmed, 2 rejected; 2 malformed" in text
    assert "- Orders: 4 submitted, 0 not_submitted, 0 error" in text
    assert "- Top rejection reasons: blocklist (2)" in text
    # The baseline reads the same fake closes the runs traded on.
    bars = offline_broker(TUESDAY).get_daily_bars(list(basket), 30)
    expected = baseline_return(bars, basket, date(2026, 9, 28), date(2026, 9, 29))
    assert expected is not None
    assert f", close before 2026-09-28 to close before 2026-09-29: {expected * 100:+.1f}%;" in text
    assert text.count("### 2026-09-2") == 2


def test_reports_on_real_runs_leave_offline_ones_out(
    engine: Engine, conn: Connection, tmp_path: Path
) -> None:
    offline_run(engine, MONDAY)

    path = write_report(
        engine,
        today=date(2026, 9, 29),
        days=7,
        paper=True,
        offline=False,
        basket=CONFIG.strategy.baseline_basket,
        broker=None,
        directory=tmp_path,
    )

    assert path.name == "week-2026-09-29.md"
    assert path.read_text(encoding="utf-8").endswith("No dry_run or submit runs in this window.\n")


def test_trader_report_offline(
    engine: Engine,
    conn: Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    offline_run(engine, utc_now())  # dated today, so it falls in the report's window
    shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
    monkeypatch.chdir(tmp_path)  # the report goes to ./reports
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.delenv("ALPACA_PAPER", raising=False)

    assert main(["report", "--offline"]) == 0

    (written,) = (tmp_path / "reports").iterdir()
    assert written.name.endswith("-offline.md")
    assert f"Wrote reports/{written.name}" in capsys.readouterr().out
    assert "## Daily log" in written.read_text(encoding="utf-8")
