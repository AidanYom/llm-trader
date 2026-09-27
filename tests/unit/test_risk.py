from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from typing import Any, cast

import pytest

from trader.models import (
    AccountState,
    Order,
    Policy,
    Position,
    Proposal,
    RiskContext,
    Side,
    StopPolicy,
    SymbolStats,
    Verdict,
    VerdictStatus,
)
from trader.risk import drawdown_pct, evaluate, freeze_active

APPROVED, TRIMMED, REJECTED = VerdictStatus.APPROVED, VerdictStatus.TRIMMED, VerdictStatus.REJECTED

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
    blocked_symbols=frozenset({"TQQQ", "SQQQ", "SOXL"}),
)


# Builders. Unless a test says otherwise: $100,000 equity and cash, no positions, every symbol closing at
# $50.00 (so the entry limit is $50.50) with $50M of average dollar volume.
def buy(
    symbol: str = "XLK",
    *,
    target: float | None = 5.0,
    stop: float | None = 5.0,
    take_profit: float | None = None,
) -> Proposal:
    return Proposal(
        symbol=symbol,
        action=Side.BUY,
        thesis="thesis",
        invalidation="invalidation",
        target_pct=target,
        stop_pct=stop,
        take_profit_pct=take_profit,
    )


def sell(symbol: str) -> Proposal:
    return Proposal(symbol=symbol, action=Side.SELL, thesis="thesis", invalidation="invalidation")


def held(symbol: str, qty: float = 10.0, price: float = 50.0) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry_price=price,
        current_price=price,
        market_value=qty * price,
        unrealized_plpc=0.0,
    )


def account(*positions: Position, equity: float = 100_000.0, cash: float = 100_000.0) -> AccountState:
    return AccountState(equity=equity, cash=cash, positions=positions)


def stat(symbol: str, close: float = 50.0, adv: float = 50_000_000.0) -> SymbolStats:
    return SymbolStats(symbol=symbol, as_of=date(2026, 9, 25), last_close=close, avg_dollar_volume_20d=adv)


STATS = {symbol: stat(symbol) for symbol in ("XLK", "XLE", "SMH", "TQQQ")}  # no stats for SOXL
SIX_HELD = tuple(held(symbol) for symbol in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"))


def run(
    *proposals: Proposal,
    acct: AccountState | None = None,
    stats: Mapping[str, SymbolStats] = STATS,
    ctx: RiskContext | None = None,
    policy: Policy = POLICY,
) -> list[Verdict]:
    return evaluate(
        proposals,
        account() if acct is None else acct,
        stats,
        RiskContext() if ctx is None else ctx,
        policy,
    )


def only(verdicts: list[Verdict]) -> Verdict:
    assert len(verdicts) == 1
    return verdicts[0]


def order_of(verdict: Verdict) -> Order:
    assert verdict.order is not None, verdict.reasons
    return verdict.order


def assert_rejected(verdict: Verdict, reason: str) -> None:
    assert (verdict.status, verdict.reasons, verdict.order) == (REJECTED, (reason,), None)


# --- HANDOFF §14's risk engine list ---


def test_buy_approved_with_exact_bracket_prices() -> None:
    verdict = only(
        run(buy("XLK", target=5, stop=6, take_profit=12), stats={"XLK": stat("XLK", close=123.40)})
    )

    assert verdict.status == APPROVED
    assert verdict.reasons == ()
    assert verdict.opens_new_position
    # limit round(123.40 × 1.01, 2), stop round(123.40 × 0.94, 2), take-profit round(123.40 × 1.12, 2),
    # and floor($5,000 / $124.63) shares.
    assert verdict.order == Order(
        symbol="XLK", side=Side.BUY, qty=40, limit_price=124.63, stop_price=116.00, take_profit_price=138.21
    )


def test_buy_without_stop_rejected() -> None:
    assert_rejected(only(run(buy(stop=None))), "stop: stop_pct is required")


@pytest.mark.parametrize(
    ("stop", "reason"), [(2.9, "stop: 2.9% is outside 3–15%"), (15.5, "stop: 15.5% is outside 3–15%")]
)
def test_buy_stop_outside_range_rejected(stop: float, reason: str) -> None:
    assert_rejected(only(run(buy(stop=stop))), reason)


@pytest.mark.parametrize(("stop", "stop_price"), [(3, 48.50), (15, 42.50)])
def test_buy_stop_at_either_end_of_range_approved(stop: float, stop_price: float) -> None:
    verdict = only(run(buy(stop=stop)))

    assert verdict.status == APPROVED
    assert order_of(verdict).stop_price == stop_price


def test_buy_trimmed_to_max_position() -> None:
    verdict = only(run(buy(target=12)))

    assert verdict.status == TRIMMED
    assert verdict.reasons == ("position cap: $12,000.00 trimmed to $8,000.00 (8% of equity)",)
    assert order_of(verdict).qty == 158  # floor($8,000 / $50.50)


def test_existing_holding_counts_toward_position_cap() -> None:
    verdict = only(run(buy(target=10), acct=account(held("XLK", qty=120))))  # $6,000 already held

    assert verdict.status == TRIMMED
    assert verdict.reasons == ("position cap: $4,000.00 trimmed to $2,000.00 (8% of equity)",)
    assert not verdict.opens_new_position
    assert order_of(verdict).qty == 39  # floor($2,000 / $50.50)


def test_buy_trimmed_to_cash_after_buffer() -> None:
    verdict = only(run(buy(target=5), acct=account(cash=7_000)))  # $7,000 − 5% of $100,000

    assert verdict.status == TRIMMED
    assert verdict.reasons == ("cash: $5,000.00 trimmed to $2,000.00 available",)
    assert order_of(verdict).qty == 39


def test_buy_below_min_price_rejected() -> None:
    verdict = only(run(buy(), stats={"XLK": stat("XLK", close=4.99)}))

    assert_rejected(verdict, "price: last close $4.99 is below $5.00")


def test_buy_below_min_liquidity_rejected() -> None:
    verdict = only(run(buy(), stats={"XLK": stat("XLK", adv=4_900_000)}))

    assert_rejected(verdict, "liquidity: 20d avg dollar volume $4.90M is below $5.00M")


def test_buy_trimmed_to_share_of_avg_dollar_volume() -> None:
    # 1% of a $5M floor is $50,000, so the cap only binds on large orders: use a $10M account.
    verdict = only(
        run(
            buy(target=5),
            acct=account(equity=10_000_000, cash=10_000_000),
            stats={"XLK": stat("XLK", adv=20_000_000)},
        )
    )

    assert verdict.status == TRIMMED
    assert verdict.reasons == (
        "liquidity cap: $500,000.00 trimmed to $200,000.00 (1% of 20d avg dollar volume)",
    )
    assert order_of(verdict).qty == 3960  # floor($200,000 / $50.50)


def test_buy_below_one_share_rejected() -> None:
    verdict = only(
        run(buy(target=5), acct=account(equity=20_000, cash=20_000), stats={"XLK": stat("XLK", close=2_000)})
    )

    assert_rejected(verdict, "size: $1,000.00 is below one share at limit $2,020.00")


def test_blocked_symbol_rejected() -> None:
    assert_rejected(only(run(buy(" tqqq"))), "blocklist: TQQQ is blocked")


def test_buy_without_market_data_rejected() -> None:
    assert_rejected(only(run(buy("ZZZZ"))), "market data: no usable price history for ZZZZ")


def test_drawdown_freeze_blocks_buys_not_sells() -> None:
    verdicts = run(
        buy("XLK"),
        sell("XLE"),
        acct=account(held("XLE"), equity=84_000, cash=80_000),
        ctx=RiskContext(equity_peak=100_000),
    )

    assert_rejected(
        verdicts[0], "drawdown freeze: equity is 16.0% below its peak $100,000.00 (freeze at 15%)"
    )
    assert verdicts[1].status == APPROVED


def test_sell_is_full_exit_at_market() -> None:
    verdict = only(run(sell("XLE"), acct=account(held("XLE", qty=12))))

    assert verdict == Verdict(
        symbol="XLE",
        action=Side.SELL,
        status=APPROVED,
        reasons=("full exit",),
        order=Order(symbol="XLE", side=Side.SELL, qty=12),
    )


def test_sell_of_fractional_holding_sells_whole_shares() -> None:
    verdict = only(run(sell("XLE"), acct=account(held("XLE", qty=10.7))))

    assert order_of(verdict).qty == 10


def test_sell_of_unheld_symbol_rejected() -> None:
    assert_rejected(only(run(sell("XYZ"))), "not held: XYZ is not held (long-only, no shorting)")


def test_sell_below_one_share_rejected() -> None:
    verdict = only(run(sell("XLE"), acct=account(held("XLE", qty=0.4))))

    assert_rejected(verdict, "size: holding of 0.4 shares is below one share")


def test_max_open_positions_rejects_new_symbol() -> None:
    assert_rejected(only(run(buy("XLK"), acct=account(*SIX_HELD))), "max open positions: 6 of 6 in use")


def test_exit_frees_slot_for_new_buy() -> None:
    verdicts = run(buy("XLK"), sell("AAA"), buy("XLE"), acct=account(*SIX_HELD))

    assert [verdict.status for verdict in verdicts] == [APPROVED, APPROVED, REJECTED]
    assert verdicts[2].reasons == ("max open positions: 6 of 6 in use",)


def test_weekly_cap_counts_earlier_buys_in_run() -> None:
    verdicts = run(buy("XLK"), buy("XLE"), ctx=RiskContext(new_positions_this_week=3))

    assert verdicts[0].status == APPROVED
    assert_rejected(verdicts[1], "weekly limit: 4 of 4 new positions already opened this week")


def test_cash_is_shared_across_buys_in_run() -> None:
    # $12,000 cash − $5,000 buffer leaves $7,000. XLK takes 99 shares × $50.50 = $4,999.50 of it.
    verdicts = run(buy("XLK"), buy("XLE"), acct=account(cash=12_000))

    assert verdicts[0].status == APPROVED
    assert order_of(verdicts[0]).qty == 99
    assert verdicts[1].status == TRIMMED
    assert verdicts[1].reasons == ("cash: $5,000.00 trimmed to $2,000.50 available",)
    assert order_of(verdicts[1]).qty == 39


def test_second_proposal_for_symbol_rejected() -> None:
    verdicts = run(buy("XLK"), buy(" xlk", target=3))

    assert verdicts[0].status == APPROVED
    assert_rejected(verdicts[1], "duplicate: XLK already has a proposal in this run")


def test_sell_wins_over_buy_for_same_symbol() -> None:
    verdicts = run(buy("XLE"), sell("XLE"), acct=account(held("XLE")))

    assert_rejected(verdicts[0], "duplicate: XLE already has a proposal in this run")
    assert verdicts[1].status == APPROVED


@pytest.mark.parametrize(
    ("target", "reason"),
    [(0, "target: target_pct must be above 0, got 0%"), (-5, "target: target_pct must be above 0, got -5%")],
)
def test_buy_with_non_positive_target_rejected(target: float, reason: str) -> None:
    assert_rejected(only(run(buy(target=target))), reason)


def test_take_profit_not_above_limit_dropped() -> None:
    verdict = only(run(buy(take_profit=1)))  # round($50 × 1.01, 2) is the entry limit itself

    assert verdict.status == APPROVED
    assert verdict.reasons == ("take-profit dropped: not above the entry limit",)
    assert order_of(verdict).take_profit_price is None


# --- Every other rule branch ---


def test_take_profit_one_cent_above_limit_kept() -> None:
    verdict = only(run(buy(take_profit=1.02)))  # round($50 × 1.0102, 2) = $50.51

    assert verdict.reasons == ()
    assert order_of(verdict).take_profit_price == 50.51


def test_buy_without_target_rejected() -> None:
    assert_rejected(only(run(buy(target=None))), "target: target_pct is required for a buy")


def test_buy_already_at_target_rejected() -> None:
    verdict = only(run(buy(target=5), acct=account(held("XLK", qty=120))))  # $6,000 is 6% of equity

    assert_rejected(verdict, "target: already at or above target_pct 5% (holding 6.0% of equity)")


def test_buy_rejected_when_holding_already_over_cap() -> None:
    verdict = only(run(buy(target=12), acct=account(held("XLK", qty=180))))  # $9,000 is 9% of equity

    assert_rejected(verdict, "position cap: already 9.0% of equity, cap 8%")


def test_buy_rejected_when_no_cash_left() -> None:
    verdict = only(run(buy(), acct=account(cash=5_000)))  # exactly the 5% buffer

    assert_rejected(verdict, "cash: nothing left after the 5% buffer and earlier buys")


def test_stop_not_below_entry_limit_rejected() -> None:
    # Only a tiny minimum stop with no entry buffer gets here: round($10 × 0.9996, 2) is $10.00.
    policy = replace(
        POLICY, entry_limit_buffer_pct=0.0, stop=StopPolicy(required=True, min_pct=0.01, max_pct=15.0)
    )
    verdict = only(run(buy(stop=0.04), stats={"XLK": stat("XLK", close=10.0)}, policy=policy))

    assert_rejected(
        verdict, "stop: stop price $10.00 must be at least $0.01 and below the entry limit $10.00"
    )


def test_adding_to_holding_skips_new_position_limits() -> None:
    six_with_xlk = (*SIX_HELD[:5], held("XLK"))
    verdict = only(
        run(buy("XLK", target=7), acct=account(*six_with_xlk), ctx=RiskContext(new_positions_this_week=4))
    )

    assert verdict.status == APPROVED
    assert not verdict.opens_new_position
    assert order_of(verdict).qty == 128  # floor(($7,000 − $500 held) / $50.50)


def test_trims_accumulate_reasons_in_check_order() -> None:
    verdict = only(run(buy(target=12), acct=account(equity=10_000_000, cash=800_000)))

    assert verdict.status == TRIMMED
    assert verdict.reasons == (
        "position cap: $1,200,000.00 trimmed to $800,000.00 (8% of equity)",
        "liquidity cap: $800,000.00 trimmed to $500,000.00 (1% of 20d avg dollar volume)",
        "cash: $500,000.00 trimmed to $300,000.00 available",
    )
    assert order_of(verdict).qty == 5940  # floor($300,000 / $50.50)


@pytest.mark.parametrize(
    ("proposal", "acct", "stats", "ctx", "expected"),
    [
        pytest.param(
            buy("TQQQ"),
            account(equity=80_000),
            STATS,
            RiskContext(equity_peak=100_000),
            "drawdown freeze:",
            id="freeze before blocklist",
        ),
        pytest.param(buy("SOXL"), account(), STATS, RiskContext(), "blocklist:", id="blocklist before data"),
        pytest.param(
            buy("ZZZZ", stop=None), account(), STATS, RiskContext(), "market data:", id="data before stop"
        ),
        pytest.param(
            buy(),
            account(),
            {"XLK": stat("XLK", close=4, adv=1_000_000)},
            RiskContext(),
            "price:",
            id="price before liquidity",
        ),
        pytest.param(
            buy(stop=None),
            account(),
            {"XLK": stat("XLK", adv=1_000_000)},
            RiskContext(),
            "liquidity:",
            id="liquidity before stop",
        ),
        pytest.param(
            buy(stop=None, target=None), account(), STATS, RiskContext(), "stop:", id="stop before target"
        ),
        pytest.param(
            buy(target=0), account(*SIX_HELD), STATS, RiskContext(), "target:", id="target before slots"
        ),
        pytest.param(
            buy(),
            account(*SIX_HELD),
            STATS,
            RiskContext(new_positions_this_week=4),
            "max open positions:",
            id="slots before weekly limit",
        ),
        pytest.param(
            buy(),
            account(cash=0),
            STATS,
            RiskContext(new_positions_this_week=4),
            "weekly limit:",
            id="weekly limit before cash",
        ),
        pytest.param(
            buy(target=5),
            account(held("XLK", qty=180)),
            STATS,
            RiskContext(),
            "target: already",
            id="sizing before position cap",
        ),
        pytest.param(
            buy(target=12),
            account(held("XLK", qty=180), cash=0),
            STATS,
            RiskContext(),
            "position cap:",
            id="position cap before cash",
        ),
        pytest.param(
            buy(),
            account(cash=5_000),
            {"XLK": stat("XLK", close=200_000)},
            RiskContext(),
            "cash:",
            id="cash before shares",
        ),
    ],
)
def test_checks_run_in_spec_order(
    proposal: Proposal,
    acct: AccountState,
    stats: Mapping[str, SymbolStats],
    ctx: RiskContext,
    expected: str,
) -> None:
    # Each case fails two checks; the earlier one in HANDOFF §7's order must give the reason.
    verdict = only(run(proposal, acct=acct, stats=stats, ctx=ctx))

    assert verdict.status == REJECTED
    assert verdict.reasons[0].startswith(expected)


def test_verdicts_follow_proposal_order() -> None:
    verdicts = run(buy("XLK"), sell("XLE"), buy("SMH"), acct=account(held("XLE")))

    assert [(verdict.symbol, verdict.action) for verdict in verdicts] == [
        ("XLK", "buy"),
        ("XLE", "sell"),
        ("SMH", "buy"),
    ]
    assert [verdict.status for verdict in verdicts] == [APPROVED, APPROVED, APPROVED]


def test_no_proposals_give_no_verdicts() -> None:
    assert run() == []


def test_symbols_are_normalized() -> None:
    verdicts = run(buy(" xlk "), sell("xle"), acct=account(held("XLE")))

    assert (verdicts[0].symbol, order_of(verdicts[0]).symbol) == ("XLK", "XLK")
    assert (verdicts[1].symbol, order_of(verdicts[1]).symbol) == ("XLE", "XLE")


def test_invalid_symbol_or_action_rejected() -> None:
    hold = Proposal(symbol="XLK", action=cast(Side, "hold"), thesis="thesis", invalidation="invalidation")
    verdicts = run(buy("BRK B"), buy(""), hold, buy("XLK"))

    assert_rejected(verdicts[0], "symbol: 'BRK B' is not a valid symbol")
    assert_rejected(verdicts[1], "symbol: '' is not a valid symbol")
    assert_rejected(verdicts[2], "action: 'hold' is not buy or sell")
    assert verdicts[3].status == APPROVED  # an invalid proposal doesn't count as XLK's first


# --- Bad input is rejected, never raised ---
# The model's JSON can carry NaN, 1e999 (infinity) or strings. These tests pass such values on purpose, so
# they're typed Any: the type checker would otherwise refuse to build the bad proposals.

GARBAGE = [math.nan, math.inf, -math.inf, "5", True, 10**400]
GARBAGE_IDS = ["nan", "inf", "-inf", "str", "bool", "huge int"]


@pytest.mark.parametrize("value", GARBAGE, ids=GARBAGE_IDS)
@pytest.mark.parametrize(("field", "category"), [("target_pct", "target:"), ("stop_pct", "stop:")])
def test_unusable_buy_number_rejected(field: str, category: str, value: Any) -> None:
    verdict = only(run(replace(buy(), **{field: value})))

    assert verdict.status == REJECTED
    assert verdict.reasons[0].startswith(category)


@pytest.mark.parametrize("value", [*GARBAGE, 1e308], ids=[*GARBAGE_IDS, "overflows"])
def test_unusable_take_profit_dropped(value: Any) -> None:
    verdict = only(run(replace(buy(), take_profit_pct=value)))

    assert verdict.status == APPROVED
    assert verdict.reasons[0].startswith("take-profit dropped:")
    assert order_of(verdict).take_profit_price is None


def test_huge_target_trimmed_to_position_cap() -> None:
    verdict = only(run(buy(target=1e308)))

    assert verdict.status == TRIMMED
    assert order_of(verdict).qty == 158  # the same as any target above 8%


def test_unusable_account_blocks_buys_not_sells() -> None:
    verdicts = run(buy("XLK"), sell("XLE"), acct=account(held("XLE"), equity=math.nan))

    assert_rejected(verdicts[0], "account: equity nan is not a positive number")
    assert verdicts[1].status == APPROVED


@pytest.mark.parametrize(
    ("ctx", "reason"),
    [
        (RiskContext(equity_peak=math.nan), "risk context: equity peak nan is not a number"),
        (
            RiskContext(new_positions_this_week=-1),
            "risk context: new positions this week -1 is not a whole number >= 0",
        ),
    ],
)
def test_unusable_risk_context_blocks_buys(ctx: RiskContext, reason: str) -> None:
    assert_rejected(only(run(buy(), ctx=ctx)), reason)


def test_unusable_market_data_rejected() -> None:
    verdict = only(run(buy(), stats={"XLK": stat("XLK", close=math.nan)}))

    assert_rejected(verdict, "market data: no usable price history for XLK")


def test_sell_with_unusable_quantity_rejected() -> None:
    verdict = only(run(sell("XLE"), acct=account(held("XLE", qty=math.nan))))

    assert_rejected(verdict, "account: XLE quantity nan is not a number")


# --- The freeze helpers M3's briefing banner will use ---


def test_no_freeze_without_equity_peak() -> None:
    assert only(run(buy(), acct=account(equity=50_000, cash=50_000))).status == APPROVED


def test_drawdown_exactly_at_freeze_limit_is_not_frozen() -> None:
    policy = replace(POLICY, drawdown_freeze_pct=25.0)  # 25% keeps the float math exact

    assert drawdown_pct(75_000, 100_000) == 25.0
    assert not freeze_active(75_000, 100_000, policy)
    assert freeze_active(74_999, 100_000, policy)
    assert drawdown_pct(120_000, 100_000) == 0.0  # a new high is the peak
