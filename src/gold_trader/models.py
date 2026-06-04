from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def direction(self) -> int:
        return 1 if self is Side.LONG else -1


@dataclass(frozen=True)
class MarketBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    spread: float = 0.0
    session: str = "unknown"
    news_distance_minutes: float | None = None
    dxy_close: float | None = None

    def true_range(self, previous_close: float | None) -> float:
        if previous_close is None:
            return self.high - self.low
        return max(
            self.high - self.low,
            abs(self.high - previous_close),
            abs(self.low - previous_close),
        )


@dataclass(frozen=True)
class TradeSignal:
    side: Side
    stop: float
    target: float
    reason: str
    tags: tuple[str, ...] = ()
    risk_reward: float = 0.0  # >0: engine recomputes target from actual fill to preserve R:R
    # Risk size scaler from the filter scoring system (see strategies/scoring.py).
    # 1.0 = full size, 0.5 = half size, 0.0 = log-only / paper-only / rejected.
    size_multiplier: float = 1.0
    # Optional confluence score 0..100 carried from the scoring layer for
    # journaling / calibration.  Zero means scoring was not applied.
    score: float = 0.0


@dataclass(frozen=True)
class BacktestConfig:
    starting_equity: float = 100_000.0
    risk_fraction: float = 0.01
    max_hold_bars: int = 24
    kill_switch_drawdown_fraction: float | None = 0.04
    commission_per_trade: float = 0.0
    # Extra slippage applied to entry+exit fills, in basis points of price.
    # 1 bp = 0.01% — gold around 2400 -> 1bp = $0.24. Realistic prop-firm
    # slippage on retail brokers is 1-3bps for limit-style fills, 5-10bps
    # for market-style fills during volatile sessions.
    slippage_bps: float = 0.0
    # When True, a TradeSignal's stop AND target are translated by the
    # signal-vs-fill price delta so the structural geometry is preserved
    # *relative to the actual fill*. The current default behavior (False)
    # only re-computes the target when risk_reward > 0; the stop stays at
    # the strategy-declared level even though the fill drifted, which
    # silently shrinks or expands risk-per-unit. Opt-in for now to avoid
    # invalidating the historical evaluation record.
    fill_aware_stops: bool = False
    # When True, the engine attaches the universal diagnostic score to
    # every signal AND uses its size_multiplier (full / half / log_only /
    # reject).  When False (default), the score is still attached for
    # journaling but the strategy's own size_multiplier (1.0 unless the
    # strategy carries internal scoring) is preserved.  Use observatory
    # output to calibrate before flipping this on in production.
    gate_universal_score: bool = False


@dataclass(frozen=True)
class ExecutedTrade:
    side: Side
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    stop: float
    target: float
    units: float
    pnl: float
    pnl_r: float
    bars_held: int
    reason: str
    exit_reason: str
    tags: tuple[str, ...] = ()
    equity_after: float = 0.0
    score: float = 0.0
    size_multiplier: float = 1.0


@dataclass(frozen=True)
class BacktestResult:
    strategy_name: str
    starting_equity: float
    ending_equity: float
    trades: tuple[ExecutedTrade, ...] = field(default_factory=tuple)
    halted_by_kill_switch: bool = False