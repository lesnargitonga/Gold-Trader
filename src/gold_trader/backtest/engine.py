from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Sequence

from ..models import BacktestConfig, BacktestResult, ExecutedTrade, MarketBar, Side, TradeSignal
from ..strategies.base import Strategy
from ..strategies.filters import universal_score as _universal_score


# Optional callback that overrides the engine-computed entry price at
# the moment a signal is being filled.  Returning ``None`` discards the
# signal entirely (used by LTF confirmation triggers that didn't fire).
# Signature: (signal, bars, index, default_entry_price) -> float | None
EntryPriceResolver = Callable[[TradeSignal, Sequence[MarketBar], int, float], "float | None"]


@dataclass
class OpenPosition:
    signal: TradeSignal
    entry_index: int
    entry_price: float
    units: float
    risk_per_unit: float
    bars_held: int = 0


def run_backtest(
    bars: Sequence[MarketBar],
    strategy: Strategy,
    config: BacktestConfig,
    *,
    entry_price_resolver: EntryPriceResolver | None = None,
) -> BacktestResult:
    equity = config.starting_equity
    peak_equity = equity
    halted_by_kill_switch = False
    open_position: OpenPosition | None = None
    trades: list[ExecutedTrade] = []

    if len(bars) <= strategy.warmup_bars() + 1:
        return BacktestResult(
            strategy_name=strategy.name,
            starting_equity=config.starting_equity,
            ending_equity=equity,
            trades=tuple(trades),
            halted_by_kill_switch=False,
        )

    for index, bar in enumerate(bars):
        if open_position is not None and index >= open_position.entry_index:
            exit_price, exit_reason = _resolve_exit(
                open_position, bar, config.max_hold_bars,
                slippage_bps=config.slippage_bps,
            )
            if exit_price is not None and exit_reason is not None:
                trade = _close_position(
                    bars=bars,
                    open_position=open_position,
                    exit_bar=bar,
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    equity_before=equity,
                    commission=config.commission_per_trade,
                )
                trades.append(trade)
                equity = trade.equity_after
                peak_equity = max(peak_equity, equity)
                open_position = None

                if (
                    config.kill_switch_drawdown_fraction is not None
                    and equity <= peak_equity * (1.0 - config.kill_switch_drawdown_fraction)
                ):
                    halted_by_kill_switch = True
            else:
                open_position.bars_held += 1

        if open_position is not None or halted_by_kill_switch:
            continue

        if index < strategy.warmup_bars() or index >= len(bars) - 1:
            continue

        signal = strategy.signal_for(bars, index)
        if signal is None:
            continue

        # Universal diagnostic score — attached to every emitted signal that
        # the strategy itself didn't already score.  The universal scorer is
        # strategy-agnostic and runs 7 well-known confluence features
        # (HTF alignment, news, weekend, session, spread, ATR regime,
        # not-overextended) summing to 100.  By default this is purely
        # observational: ``size_multiplier`` is preserved at 1.0 so emit
        # rates do not change.  Set ``BacktestConfig.gate_universal_score=True``
        # to actually gate trades on the universal verdict.
        if signal.score == 0.0:
            uscore = _universal_score(bars, index, signal.side)
            if config.gate_universal_score:
                signal = replace(
                    signal,
                    score=uscore.score,
                    size_multiplier=uscore.size_multiplier,
                )
            else:
                signal = replace(signal, score=uscore.score)

        next_bar = bars[index + 1]
        entry_price = _apply_entry_spread(signal.side, next_bar.open, next_bar.spread)
        if config.slippage_bps > 0.0:
            entry_price = _apply_slippage(
                signal.side, entry_price, config.slippage_bps, is_entry=True,
            )

        if entry_price_resolver is not None:
            override = entry_price_resolver(signal, bars, index, entry_price)
            if override is None:
                continue
            entry_price = override

        # ------------------------------------------------------------------
        # Geometry preservation modes
        # ------------------------------------------------------------------
        # 1) fill_aware_stops (opt-in, recommended for thin edges):
        #    translate BOTH stop and target by (entry_price - signal_ref).
        #    The signal is a recommendation made on bar[i].close; the actual
        #    fill happens on bar[i+1].open ± half-spread ± slippage. Without
        #    translation, the realised risk-per-unit silently differs from
        #    the structural intent — fatal for PF~1.2 edges.
        # 2) risk_reward > 0 (legacy, default for ARB/dxy_lead_lag):
        #    keep stop fixed, recompute target so RR matches declared intent.
        # ------------------------------------------------------------------
        if config.fill_aware_stops:
            # The signal-level reference price is bar[index].close (per the
            # strategies in this repo). Translate the entire geometry.
            signal_ref = bars[index].close
            shift = entry_price - signal_ref
            new_stop = signal.stop + shift
            new_target = signal.target + shift
            signal = replace(signal, stop=new_stop, target=new_target)
        elif signal.risk_reward > 0.0:
            stop_distance = abs(entry_price - signal.stop)
            if stop_distance > 0.0:
                if signal.side is Side.LONG:
                    new_target = entry_price + stop_distance * signal.risk_reward
                else:
                    new_target = entry_price - stop_distance * signal.risk_reward
                signal = replace(signal, target=new_target)

        if not _is_trade_valid(signal, entry_price):
            continue

        risk_per_unit = abs(entry_price - signal.stop)
        if risk_per_unit <= 0.0:
            continue

        units = (equity * config.risk_fraction * signal.size_multiplier) / risk_per_unit
        if units <= 0.0:
            continue

        open_position = OpenPosition(
            signal=signal,
            entry_index=index + 1,
            entry_price=entry_price,
            units=units,
            risk_per_unit=risk_per_unit,
        )

    return BacktestResult(
        strategy_name=strategy.name,
        starting_equity=config.starting_equity,
        ending_equity=equity,
        trades=tuple(trades),
        halted_by_kill_switch=halted_by_kill_switch,
    )


def _resolve_exit(
    open_position: OpenPosition,
    bar: MarketBar,
    max_hold_bars: int,
    slippage_bps: float = 0.0,
) -> tuple[float | None, str | None]:
    signal = open_position.signal

    def _exit(price: float) -> float:
        p = _apply_exit_spread(signal.side, price, bar.spread)
        if slippage_bps > 0.0:
            p = _apply_slippage(signal.side, p, slippage_bps, is_entry=False)
        return p

    if signal.side is Side.LONG:
        if bar.low <= signal.stop:
            return _exit(signal.stop), "stop"
        if bar.high >= signal.target:
            return _exit(signal.target), "target"
    else:
        if bar.high >= signal.stop:
            return _exit(signal.stop), "stop"
        if bar.low <= signal.target:
            return _exit(signal.target), "target"

    if open_position.bars_held + 1 >= max_hold_bars:
        return _exit(bar.close), "time"

    return None, None


def _close_position(
    bars: Sequence[MarketBar],
    open_position: OpenPosition,
    exit_bar: MarketBar,
    exit_price: float,
    exit_reason: str,
    equity_before: float,
    commission: float = 0.0,
) -> ExecutedTrade:
    signal = open_position.signal
    entry_bar = bars[open_position.entry_index]
    price_move = (exit_price - open_position.entry_price) * signal.side.direction
    pnl = price_move * open_position.units - commission
    pnl_r = pnl / (open_position.risk_per_unit * open_position.units)

    return ExecutedTrade(
        side=signal.side,
        entry_time=entry_bar.timestamp,
        exit_time=exit_bar.timestamp,
        entry_price=open_position.entry_price,
        exit_price=exit_price,
        stop=signal.stop,
        target=signal.target,
        units=open_position.units,
        pnl=pnl,
        pnl_r=pnl_r,
        bars_held=open_position.bars_held + 1,
        reason=signal.reason,
        exit_reason=exit_reason,
        tags=signal.tags,
        equity_after=equity_before + pnl,
        score=signal.score,
        size_multiplier=signal.size_multiplier,
    )


def _is_trade_valid(signal: TradeSignal, entry_price: float) -> bool:
    if signal.side is Side.LONG:
        return signal.stop < entry_price < signal.target
    return signal.target < entry_price < signal.stop


def _apply_entry_spread(side: Side, price: float, spread: float) -> float:
    half_spread = spread / 2.0
    if side is Side.LONG:
        return price + half_spread
    return price - half_spread


def _apply_exit_spread(side: Side, price: float, spread: float) -> float:
    half_spread = spread / 2.0
    if side is Side.LONG:
        return price - half_spread
    return price + half_spread


def _apply_slippage(
    side: Side, price: float, slippage_bps: float, is_entry: bool,
) -> float:
    """Apply adverse slippage in basis points of the fill price.

    Slippage is *always adverse*: entries fill worse, exits fill worse.
    1 bp = 0.0001 of price.  Realistic settings: 1-3bp for limit fills,
    5-10bp for market fills, 20+bp during high-volatility events.
    """
    delta = price * slippage_bps / 10_000.0
    if side is Side.LONG:
        return price + delta if is_entry else price - delta
    return price - delta if is_entry else price + delta