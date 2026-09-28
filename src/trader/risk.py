"""The risk engine (HANDOFF §7): approves, trims or rejects each proposal against the policy's hard limits.

Pure and deterministic, with no I/O. It never raises on bad input: a proposal with a missing or unusable
field is rejected with a reason, and so is every buy when the account or risk context can't be sized
against. Every trim and rejection carries a reason of the form "category: detail", and the weekly report
groups rejections by the category, the text before the first colon.

It builds every order run.py sends (CLAUDE.md invariant 2): those in the verdicts on the day's proposals,
and, through `restored_stop()`, the stop for the shares of a cancelled, partially filled entry.
"""

from __future__ import annotations

import functools
import math
import re
from collections.abc import Mapping, Sequence

from trader.models import (
    AccountState,
    Order,
    Policy,
    Proposal,
    RiskContext,
    Side,
    SymbolStats,
    Verdict,
    VerdictStatus,
    finite_float,
    normalize_symbol,
    to_cents,
)


def evaluate(
    proposals: Sequence[Proposal],
    account: AccountState,
    stats: Mapping[str, SymbolStats],
    ctx: RiskContext,
    policy: Policy,
    asset_names: Mapping[str, str],
) -> list[Verdict]:
    """Decide every proposal, and return the verdicts in proposal order.

    Sells are evaluated before buys, so approved exits free position slots; each side keeps the proposals'
    order. The first proposal for a symbol in that evaluation order is the one evaluated, and later ones are
    rejected as duplicates, so a sell beats a buy for the same symbol.

    `asset_names` holds the broker's name for each proposed buy's symbol, which is checked for leveraged and
    inverse funds. A buy with no name is rejected, since it can't be checked.
    """
    engine = _Engine(account, stats, ctx, policy, asset_names)
    sells = [i for i, proposal in enumerate(proposals) if proposal.action == Side.SELL]
    buys = [i for i, proposal in enumerate(proposals) if proposal.action == Side.BUY]
    invalid = sorted(set(range(len(proposals))) - set(sells) - set(buys))
    verdicts: dict[int, Verdict] = {}
    for i in sells + buys + invalid:  # the order matters: the engine tracks slots, cash and symbols seen
        verdicts[i] = engine.decide(proposals[i])
    return [verdicts[i] for i in range(len(proposals))]


def restored_stop(symbol: str, filled_qty: float, stop_price: float | None) -> Order | None:
    """The protective stop for the shares a cancelled, partially filled entry bought (HANDOFF §8).

    The entry's legs only activate once it fills completely, and cancelling it cancels them, so its shares
    would be left with no stop. This re-places the stop the engine approved for that entry, for the whole
    shares it filled. None when there's nothing to place: no whole share, or no usable stop price.
    """
    qty = finite_float(filled_qty)
    stop = to_cents(stop_price)
    if qty is None or qty < 1 or stop is None or stop < 1:
        return None
    return Order(symbol=symbol, side=Side.SELL, qty=math.floor(qty), stop_price=stop / 100)


def blocked_pattern(name: str, patterns: Sequence[str]) -> str | None:
    """The first pattern that appears in the asset name as whole words, ignoring case; None if none does.

    Whole words: the characters just before and after the match aren't letters or digits, and runs of
    whitespace count as one space. So "3X" matches "Bull 3X Shares" and "-3X", "ProShares Ultra" doesn't match
    "ProShares UltraPro QQQ", and "Bear" doesn't match "Bearish".
    """
    text = " ".join(name.split())
    return next((pattern for pattern in patterns if _whole_words(pattern).search(text)), None)


@functools.cache
def _whole_words(pattern: str) -> re.Pattern[str]:
    words = re.escape(" ".join(pattern.split()))
    return re.compile(rf"(?<![A-Za-z0-9]){words}(?![A-Za-z0-9])", re.IGNORECASE)


def drawdown_pct(equity: float, equity_peak: float | None) -> float:
    """How far equity is below its peak, in %: 0 at a new high or before there's any history."""
    peak = _peak(equity, equity_peak)
    return (peak - equity) / peak * 100 if peak > 0 else 0.0


def freeze_active(equity: float, equity_peak: float | None, policy: Policy) -> bool:
    """HANDOFF §7 step 1: buys stop while equity < max(peak, equity) × (1 − drawdown_freeze_pct / 100)."""
    return equity < _peak(equity, equity_peak) * (1 - policy.drawdown_freeze_pct / 100)


class _Engine:
    """Evaluates one run's proposals, tracking what earlier approvals in the run have used up."""

    def __init__(
        self,
        account: AccountState,
        stats: Mapping[str, SymbolStats],
        ctx: RiskContext,
        policy: Policy,
        asset_names: Mapping[str, str],
    ) -> None:
        self.account = account
        self.stats = stats
        self.ctx = ctx
        self.policy = policy
        self.asset_names = asset_names
        self.held = {_shown(position.symbol): position for position in account.positions}
        self.seen: set[str] = set()
        self.open_positions = len(account.positions)  # current − approved exits + approved new
        self.new_positions = ctx.new_positions_this_week  # this week's, plus approved new this run
        self.committed = 0.0  # cash committed by buys approved earlier in this run
        self.buy_blocker = _unusable_inputs(account, ctx)

    def decide(self, proposal: Proposal) -> Verdict:
        symbol = normalize_symbol(proposal.symbol)
        if symbol is None:
            return _rejected(
                _shown(proposal.symbol),
                str(proposal.action),
                f"symbol: {proposal.symbol!r} is not a valid symbol",
            )
        if proposal.action not in (Side.BUY, Side.SELL):
            return _rejected(symbol, str(proposal.action), f"action: {proposal.action!r} is not buy or sell")
        side = Side(proposal.action)
        if symbol in self.seen:
            return _rejected(symbol, side, f"duplicate: {symbol} already has a proposal in this run")
        self.seen.add(symbol)
        return self._sell(symbol) if side == Side.SELL else self._buy(symbol, proposal)

    def _sell(self, symbol: str) -> Verdict:
        position = self.held.get(symbol)
        if position is None:
            return _rejected(symbol, Side.SELL, f"not held: {symbol} is not held (long-only, no shorting)")
        qty = finite_float(position.qty)
        if qty is None:
            return _rejected(
                symbol, Side.SELL, f"account: {symbol} quantity {position.qty!r} is not a number"
            )
        if qty < 1:
            return _rejected(symbol, Side.SELL, f"size: holding of {qty:g} shares is below one share")
        self.open_positions -= 1
        return Verdict(
            symbol=symbol,
            action=Side.SELL,
            status=VerdictStatus.APPROVED,
            reasons=("full exit",),
            order=Order(symbol=symbol, side=Side.SELL, qty=math.floor(qty)),
        )

    def _buy(self, symbol: str, proposal: Proposal) -> Verdict:
        policy = self.policy

        def reject(reason: str) -> Verdict:
            return _rejected(symbol, Side.BUY, reason)

        if self.buy_blocker is not None:
            return reject(self.buy_blocker)
        equity = self.account.equity

        # 1. Drawdown freeze. Sells never reach this check.
        peak = self.ctx.equity_peak
        if freeze_active(equity, peak, policy):
            return reject(
                f"drawdown freeze: equity is {drawdown_pct(equity, peak):.1f}% below its peak "
                f"{_usd(_peak(equity, peak))} (freeze at {_pct(policy.drawdown_freeze_pct)})"
            )

        # 2. Blocklist: the symbol, then the asset name, which catches leveraged and inverse funds the symbol
        # list can't name.
        if symbol in policy.blocked_symbols:
            return reject(f"blocklist: {symbol} is blocked")
        name = self.asset_names.get(symbol)
        pattern = None if name is None else blocked_pattern(name, policy.blocked_name_patterns)
        if pattern is not None:
            return reject(f'blocklist: {symbol}\'s name "{name}" matches the blocked pattern "{pattern}"')

        # 3. Market data: price history, and the asset name step 2 needs.
        stat = self.stats.get(symbol)
        last_close = finite_float(stat.last_close) if stat else None
        adv = finite_float(stat.avg_dollar_volume_20d) if stat else None
        if last_close is None or last_close <= 0 or adv is None or adv < 0:
            return reject(f"market data: no usable price history for {symbol}")
        if name is None:
            return reject(f"market data: no asset name for {symbol}, so it can't be checked for leverage")

        # 4. Price.
        if last_close < policy.min_price:
            return reject(f"price: last close {_usd(last_close)} is below {_usd(policy.min_price)}")

        # 5. Liquidity.
        if adv < policy.min_avg_dollar_volume:
            return reject(
                f"liquidity: 20d avg dollar volume {_usd_millions(adv)} is below "
                f"{_usd_millions(policy.min_avg_dollar_volume)}"
            )

        # 6. Stop. Required on every buy (CLAUDE.md invariant 3); the loader refuses stop.required: false.
        if proposal.stop_pct is None:
            return reject("stop: stop_pct is required")
        stop_pct = finite_float(proposal.stop_pct)
        if stop_pct is None:
            return reject(f"stop: stop_pct {proposal.stop_pct!r} is not a number")
        if not policy.stop.min_pct <= stop_pct <= policy.stop.max_pct:
            return reject(
                f"stop: {_pct(stop_pct)} is outside {policy.stop.min_pct:g}–{_pct(policy.stop.max_pct)}"
            )

        # 7. Target.
        if proposal.target_pct is None:
            return reject("target: target_pct is required for a buy")
        target_pct = finite_float(proposal.target_pct)
        if target_pct is None:
            return reject(f"target: target_pct {proposal.target_pct!r} is not a number")
        if target_pct <= 0:
            return reject(f"target: target_pct must be above 0, got {_pct(target_pct)}")

        # 8. New-position limits, when the symbol isn't held yet.
        position = self.held.get(symbol)
        opens_new = position is None
        if opens_new:
            if self.open_positions >= policy.max_open_positions:
                return reject(
                    f"max open positions: {self.open_positions} of {policy.max_open_positions} in use"
                )
            if self.new_positions >= policy.max_new_positions_per_week:
                return reject(
                    f"weekly limit: {self.new_positions} of {policy.max_new_positions_per_week} "
                    "new positions already opened this week"
                )

        # 9. Sizing: what it takes to bring the holding up to target_pct.
        existing = position.market_value if position else 0.0
        notional = _pct_of(target_pct, equity) - existing
        if notional <= 0:
            return reject(
                f"target: already at or above target_pct {_pct(target_pct)} "
                f"(holding {existing / equity * 100:.1f}% of equity)"
            )
        trims: list[str] = []

        # 10. Position cap, existing holding included.
        room = _pct_of(policy.max_position_pct, equity) - existing
        if room <= 0:
            return reject(
                f"position cap: already {existing / equity * 100:.1f}% of equity, "
                f"cap {_pct(policy.max_position_pct)}"
            )
        if notional > room:
            trims.append(
                f"position cap: {_usd(notional)} trimmed to {_usd(room)} "
                f"({_pct(policy.max_position_pct)} of equity)"
            )
            notional = room

        # 11. Liquidity cap.
        adv_cap = _pct_of(policy.max_pct_of_adv, adv)
        if notional > adv_cap:
            trims.append(
                f"liquidity cap: {_usd(notional)} trimmed to {_usd(adv_cap)} "
                f"({_pct(policy.max_pct_of_adv)} of 20d avg dollar volume)"
            )
            notional = adv_cap

        # 12. Cash. Proceeds from sells in this run never fund buys.
        available = self.account.cash - _pct_of(policy.min_cash_buffer_pct, equity) - self.committed
        if available <= 0:
            return reject(
                f"cash: nothing left after the {_pct(policy.min_cash_buffer_pct)} buffer and earlier buys"
            )
        if notional > available:
            trims.append(f"cash: {_usd(notional)} trimmed to {_usd(available)} available")
            notional = available

        # 13. Whole shares at the entry limit.
        limit = round(last_close * (1 + policy.entry_limit_buffer_pct / 100), 2)
        limit_cents = to_cents(limit)
        if limit_cents is None or limit_cents < 1:
            return reject(f"price: entry limit {_usd(limit)} is not a usable price")
        qty = math.floor(notional / limit)
        if qty < 1:
            return reject(f"size: {_usd(notional)} is below one share at limit {_usd(limit)}")

        # 14. Protective prices, compared in whole cents. With a sane policy the stop is always well below
        # the limit; the check is there so no policy value can make the engine build an order Alpaca rejects.
        stop_price = round(last_close * (1 - stop_pct / 100), 2)
        stop_cents = to_cents(stop_price)
        if stop_cents is None or not 1 <= stop_cents < limit_cents:
            return reject(
                f"stop: stop price {_usd(stop_price)} must be at least $0.01 and below the entry limit "
                f"{_usd(limit)}"
            )
        take_profit_price, note = _take_profit(proposal.take_profit_pct, last_close, limit_cents)

        # 15. The order, and what it uses up for the rest of the run. Dropping a take-profit isn't a trim.
        order = Order(
            symbol=symbol,
            side=Side.BUY,
            qty=qty,
            limit_price=limit,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
        )
        self.committed += qty * limit
        if opens_new:
            self.open_positions += 1
            self.new_positions += 1
        return Verdict(
            symbol=symbol,
            action=Side.BUY,
            status=VerdictStatus.TRIMMED if trims else VerdictStatus.APPROVED,
            reasons=(*trims, note) if note else tuple(trims),
            opens_new_position=opens_new,
            order=order,
        )


def _take_profit(
    take_profit_pct: object, last_close: float, limit_cents: int
) -> tuple[float | None, str | None]:
    """HANDOFF §7 step 14: the take-profit price to use, or None and why it was dropped."""
    if take_profit_pct is None:
        return None, None
    pct = finite_float(take_profit_pct)
    if pct is None:
        return None, f"take-profit dropped: take_profit_pct {take_profit_pct!r} is not a number"
    price = round(last_close * (1 + pct / 100), 2)
    cents = to_cents(price)
    if cents is None:
        return None, f"take-profit dropped: take_profit_pct {pct:g} gives no usable price"
    if cents <= limit_cents:
        return None, "take-profit dropped: not above the entry limit"
    return price, None


def _unusable_inputs(account: AccountState, ctx: RiskContext) -> str | None:
    """Why no buy can be sized against this account and context, or None. Sells don't depend on these."""
    if (equity := finite_float(account.equity)) is None or equity <= 0:
        return f"account: equity {account.equity!r} is not a positive number"
    if finite_float(account.cash) is None:
        return f"account: cash {account.cash!r} is not a number"
    for position in account.positions:
        if finite_float(position.market_value) is None:
            return f"account: {position.symbol} market value {position.market_value!r} is not a number"
    if ctx.equity_peak is not None and finite_float(ctx.equity_peak) is None:
        return f"risk context: equity peak {ctx.equity_peak!r} is not a number"
    count = ctx.new_positions_this_week
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return f"risk context: new positions this week {count!r} is not a whole number >= 0"
    return None


def _peak(equity: float, equity_peak: float | None) -> float:
    return equity if equity_peak is None else max(equity_peak, equity)


def _pct_of(pct: float, amount: float) -> float:
    return amount * pct / 100


def _rejected(symbol: str, action: str, reason: str) -> Verdict:
    return Verdict(symbol=symbol, action=action, status=VerdictStatus.REJECTED, reasons=(reason,))


def _shown(value: object) -> str:
    """A symbol as it appears in a verdict: stripped and uppercased, even when it isn't valid."""
    return value.strip().upper() if isinstance(value, str) else str(value)


def _usd(amount: float) -> str:
    return f"${amount:,.2f}"


def _usd_millions(amount: float) -> str:
    return f"${amount / 1e6:,.2f}M"


def _pct(value: float) -> str:
    """A percentage without trailing zeros: 8%, 2.5%."""
    return f"{value:g}%"
