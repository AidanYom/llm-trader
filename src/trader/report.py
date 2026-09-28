"""The weekly review (HANDOFF §11): a markdown report on recent runs, which Aidan reads in his Claude Project.

It only reads: the database through repo.py, and daily bars for the baseline through a Broker (AlpacaBroker
for real runs, FakeBroker for offline ones). `gather()` does the reading and `render()` is a pure function of
what it gathered.

- The report covers one account type, the one ALPACA_PAPER names, and dry_run and submit runs; or offline runs
  alone, with --offline.
- Run counts include every status. Equity, behavior and the daily log's detail come from completed runs,
  and each failed or abandoned run gets one daily-log line with its error.
- The API cost totals every run in the window, because failed runs record what they spent too.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import Connection, Engine

from trader.brokers.base import Broker, BrokerError
from trader.db import repo
from trader.db.repo import ReportMalformed, ReportOrder, ReportProposal, ReportRun
from trader.models import Bar, OrderStatus, Position, RunMode, RunStatus, VerdictStatus, finite_float

REPORTS_DIR = Path("reports")
SPARE_SESSIONS = 10  # bars fetched beyond the window, so the close before its first run is there
TOP_REJECTIONS = 5


@dataclass(frozen=True, slots=True, kw_only=True)
class EquityPoint:
    day: date
    equity: float
    cash: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Baseline:
    """The equal-weight basket's buy-and-hold return over the report's run dates, or why there's none."""

    basket: tuple[str, ...]
    start: date | None = None  # it runs from the last close before `start` to the last close before `end`
    end: date | None = None
    value: float | None = None  # a fraction: 0.012 is +1.2%
    missing: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportData:
    first_day: date
    last_day: date
    paper: bool
    offline: bool
    runs: list[ReportRun]
    proposals: list[ReportProposal]  # of completed runs
    malformed: list[ReportMalformed]
    orders: list[ReportOrder]
    research_calls: dict[UUID, int]
    positions: list[Position]  # from the last completed run's snapshot
    baseline: Baseline


def report_path(last_day: date, *, offline: bool, directory: Path = REPORTS_DIR) -> Path:
    return directory / f"week-{last_day.isoformat()}{'-offline' if offline else ''}.md"


def write_report(
    engine: Engine,
    *,
    today: date,
    days: int,
    paper: bool,
    offline: bool,
    basket: Sequence[str],
    broker: Broker | None,
    baseline: bool = True,
    directory: Path = REPORTS_DIR,
) -> Path:
    """Gather, render and write the report for the `days` New York dates ending `today`; return its path."""
    with engine.connect() as conn:
        repo.check_schema(conn)
        data = gather(
            conn,
            last_day=today,
            days=days,
            paper=paper,
            offline=offline,
            basket=basket,
            broker=broker,
            baseline=baseline,
        )
    path = report_path(today, offline=offline, directory=directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data), encoding="utf-8")
    return path


def gather(
    conn: Connection,
    *,
    last_day: date,
    days: int,
    paper: bool,
    offline: bool,
    basket: Sequence[str],
    broker: Broker | None,
    baseline: bool,
) -> ReportData:
    first_day = last_day - timedelta(days=max(days, 1) - 1)
    modes = [RunMode.OFFLINE] if offline else [RunMode.DRY_RUN, RunMode.SUBMIT]
    runs = repo.report_runs(conn, first_day=first_day, last_day=last_day, paper=paper, modes=modes)
    completed = [run.run_id for run in runs if run.status is RunStatus.COMPLETED]
    with_snapshot = [run for run in runs if run.status is RunStatus.COMPLETED and run.equity is not None]
    series = equity_series(runs)
    if not baseline:
        basket_return = Baseline(basket=tuple(basket), missing="skipped (--no-baseline)")
    else:
        basket_return = _baseline(broker, tuple(basket), series, last_day)
    return ReportData(
        first_day=first_day,
        last_day=last_day,
        paper=paper,
        offline=offline,
        runs=runs,
        proposals=repo.report_proposals(conn, completed),
        malformed=repo.report_malformed(conn, completed),
        orders=repo.report_orders(conn, completed),
        research_calls=repo.report_research_calls(conn, completed),
        positions=repo.report_positions(conn, with_snapshot[-1].run_id) if with_snapshot else [],
        baseline=basket_return,
    )


def equity_series(runs: Sequence[ReportRun]) -> list[EquityPoint]:
    """Completed runs' pre-market equity, one point per date: the day's last completed run with a snapshot."""
    by_day: dict[date, EquityPoint] = {}
    for run in runs:  # in start order, so a later run on the same date wins
        if run.status is RunStatus.COMPLETED and run.equity is not None:
            by_day[run.run_date] = EquityPoint(day=run.run_date, equity=run.equity, cash=run.cash)
    return [by_day[day] for day in sorted(by_day)]


def average_invested(series: Sequence[EquityPoint]) -> float | None:
    """The mean share of equity invested (equity minus cash, over equity), where both are known.

    Snapshot numbers are finite: repo.py stores a non-finite one as NULL, and such a run has no point.
    """
    shares = [
        (point.equity - point.cash) / point.equity
        for point in series
        if point.cash is not None and point.equity > 0
    ]
    return sum(shares) / len(shares) if shares else None


def baseline_return(
    bars: Mapping[str, Sequence[Bar]], basket: Sequence[str], start: date, end: date
) -> float | None:
    """HANDOFF §11: an equal-weight buy-and-hold of the basket, from the last close before `start` to the last
    close before `end`, matching the pre-market equity snapshots. None if any ETF lacks those closes."""
    returns = []
    for symbol in basket:
        closes = bars.get(symbol, ())
        before_start = [bar.close for bar in closes if bar.day < start]
        before_end = [bar.close for bar in closes if bar.day < end]
        if not before_start or not before_end or before_start[-1] <= 0:
            return None
        returns.append(before_end[-1] / before_start[-1] - 1)
    return sum(returns) / len(returns) if returns else None


def _baseline(
    broker: Broker | None, basket: tuple[str, ...], series: Sequence[EquityPoint], today: date
) -> Baseline:
    if not series:
        return Baseline(basket=basket, missing="n/a: no completed run with an equity snapshot")
    start, end = series[0].day, series[-1].day
    if broker is None:
        return Baseline(basket=basket, start=start, end=end, missing="n/a: no broker to read closes from")
    try:
        bars = broker.get_daily_bars(list(basket), (today - start).days + SPARE_SESSIONS)
    except BrokerError as exc:  # the report still renders, and says why the baseline is missing
        return Baseline(basket=basket, start=start, end=end, missing=f"n/a: {exc}")
    value = baseline_return(bars, basket, start, end)
    missing = None if value is not None else "n/a: the broker lacks closes for part of the basket"
    return Baseline(basket=basket, start=start, end=end, value=value, missing=missing)


# ---- Rendering ---------------------------------------------------------------------------------------------


def render(data: ReportData) -> str:
    covers = "offline runs" if data.offline else f"{'paper' if data.paper else 'live'} account"
    title = f"# Weekly review: {data.first_day.isoformat()} to {data.last_day.isoformat()} ({covers})"
    if not data.runs:
        modes = "offline" if data.offline else "dry_run or submit"
        sections = [[f"No {modes} runs in this window."]]
    else:
        sections = [_scorecard(data), _behavior(data), _positions(data), _daily_log(data)]
    return "\n\n".join([title, *("\n".join(section) for section in sections)]) + "\n"


def _scorecard(data: ReportData) -> list[str]:
    statuses = list(RunStatus)
    counts = Counter((run.mode, run.status) for run in data.runs)
    modes = [mode for mode in RunMode if any(run.mode is mode for run in data.runs)]
    lines = [
        "## Scorecard",
        "",
        "| Mode | " + " | ".join(status.value.capitalize() for status in statuses) + " |",
        "|---|" + "---:|" * len(statuses),
        *(
            f"| {mode.value} | " + " | ".join(str(counts[mode, status]) for status in statuses) + " |"
            for mode in modes
        ),
        "",
    ]
    series = equity_series(data.runs)
    cost = sum(run.cost_usd for run in data.runs)
    if series:
        first, last = series[0], series[-1]
        change = _change(first.equity, last.equity)
        lines.append(
            f"- Equity: {_usd(first.equity)} on {first.day.isoformat()} → {_usd(last.equity)} on "
            f"{last.day.isoformat()} ({_signed_pct(change)})"
        )
        invested = average_invested(series)
        lines.append(
            "- Average invested: n/a"
            if invested is None
            else f"- Average invested: {invested * 100:.1f}% of equity (the baseline is 100%)"
        )
        peak, drawdown = peak_and_worst_drawdown([point.equity for point in series])
        lines.append(f"- Peak equity {_usd(peak)}; worst drawdown in the window {drawdown * 100:.1f}%")
        lines.append(f"- API cost: {_usd(cost)} ({_share(cost, last.equity)} of equity)")
    else:
        change = None
        lines += ["- Equity: n/a (no completed run with an equity snapshot)", f"- API cost: {_usd(cost)}"]
    baseline = data.baseline
    span = (
        f", close before {baseline.start.isoformat()} to close before {baseline.end.isoformat()}"
        if baseline.start and baseline.end
        else ""
    )
    if baseline.value is None:
        lines.append(f"- Baseline (equal-weight {', '.join(baseline.basket)}){span}: {baseline.missing}")
    else:
        excess = "n/a" if change is None else f"{(change - baseline.value) * 100:+.1f} points"
        lines.append(
            f"- Baseline (equal-weight {', '.join(baseline.basket)}){span}: {_signed_pct(baseline.value)}; "
            f"the account's excess: {excess}"
        )
    return lines


def _behavior(data: ReportData) -> list[str]:
    completed = [run for run in data.runs if run.status is RunStatus.COMPLETED]
    never_submitted = sum(1 for run in completed if run.agent_submitted is False)
    verdicts = Counter(proposal.status for proposal in data.proposals)
    orders = Counter(order.status for order in data.orders)
    calls = [data.research_calls.get(run.run_id, 0) for run in completed]
    rejections = Counter(
        reason.split(":", 1)[0]
        for proposal in data.proposals
        if proposal.status is VerdictStatus.REJECTED
        for reason in proposal.reasons
    )
    prompts = Counter(run.prompt_version or "none" for run in completed)
    models = Counter(run.model for run in completed)
    top = sorted(rejections.items(), key=lambda item: (-item[1], item[0]))[:TOP_REJECTIONS]
    return [
        "## Behavior",
        "",
        "- Proposals: "
        + ", ".join(f"{verdicts[status]} {status.value}" for status in VerdictStatus)
        + f"; {len(data.malformed)} malformed",
        f"- Runs where the model never submitted: {never_submitted}",
        "- Orders: " + ", ".join(f"{orders[status]} {status.value}" for status in OrderStatus),
        f"- Research tool calls per run: {sum(calls) / len(calls):.1f} on average, {max(calls)} at most"
        if calls
        else "- Research tool calls per run: n/a",
        f"- Prompt versions: {_tally(prompts)}; models: {_tally(models)}",
        "- Top rejection reasons: " + (", ".join(f"{name} ({count})" for name, count in top) or "none"),
    ]


def _positions(data: ReportData) -> list[str]:
    snapshots = [run for run in data.runs if run.status is RunStatus.COMPLETED and run.equity is not None]
    if not snapshots:
        return ["## Current positions", "", "No account snapshot in this window."]
    lines = [f"## Current positions (from the {snapshots[-1].run_date.isoformat()} snapshot)", ""]
    if not data.positions:
        return [*lines, "No open positions."]
    lines += ["| Symbol | Qty | Avg entry | Last | P&L % | Market value |", "|---|---:|---:|---:|---:|---:|"]
    for position in data.positions:
        number = finite_float(position.qty)
        qty = "n/a" if number is None else f"{number:g}"
        lines.append(
            f"| {position.symbol} | {qty} | {_usd(position.avg_entry_price)} "
            f"| {_usd(position.current_price)} | {_signed_pct(position.unrealized_plpc)} "
            f"| {_usd(position.market_value)} |"
        )
    return lines


def _daily_log(data: ReportData) -> list[str]:
    proposals: dict[UUID, list[ReportProposal]] = {}
    for proposal in data.proposals:
        proposals.setdefault(proposal.run_id, []).append(proposal)
    malformed: dict[UUID, list[str]] = {}
    for item in data.malformed:
        malformed.setdefault(item.run_id, []).append(item.error)
    lines = ["## Daily log"]
    for run in data.runs:
        heading = f"### {run.run_date.isoformat()} · {run.mode.value}"
        if run.status is RunStatus.SKIPPED:
            lines += ["", f"{heading} · skipped: {run.skip_reason}"]
            continue
        if run.status is not RunStatus.COMPLETED:
            lines += [
                "",
                f"{heading} · {run.status.value} · {_run_cost(run.cost_usd)}",
                "",
                run.error or "(no error text)",
            ]
            continue
        lines += ["", f"{heading} · {_run_cost(run.cost_usd)}", ""]
        if not run.agent_submitted:
            lines.append("The model never called submit_proposals.")
            continue
        lines += [run.market_view or "(no market view)", ""]
        for proposal in proposals.get(run.run_id, []):
            reasons = "".join(f" · {reason}" for reason in proposal.reasons)
            lines += [
                f"- {_proposal_label(proposal)}: {proposal.status.value}{reasons}",
                f"  - Thesis: {proposal.thesis}",
                f"  - Invalidation: {proposal.invalidation}",
            ]
        lines += [f"- Malformed: {error}" for error in malformed.get(run.run_id, [])]
    return lines


def _proposal_label(proposal: ReportProposal) -> str:
    label = f"{proposal.action.value} {proposal.symbol}"
    if proposal.target_pct is not None:
        label += f" {proposal.target_pct:g}%"
    if proposal.stop_pct is not None:
        label += f" (stop {proposal.stop_pct:g}%)"
    return label


# ---- Math and formatting -----------------------------------------------------------------------------------


def peak_and_worst_drawdown(equities: Sequence[float]) -> tuple[float, float]:
    """The series' highest equity, and its largest fall from a running peak, as a fraction."""
    peak = worst = 0.0
    running = -math.inf
    for equity in equities:
        running = max(running, equity)
        peak = max(peak, equity)
        if running > 0:
            worst = max(worst, (running - equity) / running)
    return peak, worst


def _change(start: float, end: float) -> float | None:
    return end / start - 1 if start > 0 else None


def _usd(amount: float | None) -> str:
    number = finite_float(amount)
    return "n/a" if number is None else f"${number:,.2f}"


def _run_cost(amount: float) -> str:
    """One run's cost, to the hundredth of a cent, as the run summary shows it."""
    return f"${amount:,.4f}"


def _signed_pct(fraction: float | None) -> str:
    number = finite_float(fraction)
    return "n/a" if number is None else f"{number * 100:+.1f}%"


def _share(amount: float, whole: float) -> str:
    return f"{amount / whole * 100:.2f}%" if whole > 0 else "n/a"


def _tally(counts: Mapping[str, int]) -> str:
    return (
        ", ".join(f"{name} ({count} run{'s' if count != 1 else ''})" for name, count in counts.items())
        or "none"
    )
