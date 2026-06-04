"""MomentumContinuationStrategy.

Empirical finding from the 2026-05-08 pattern-mining sweep:

* `near_20_high & trend_up` (close within 0.30 ATR of the 20-bar high while
  EMA20 > EMA50) is the most replicating long-bias edge across 15m / 60m /
  240m timeframes (avg holdout R = +0.65, min p = 0.008, thirds-stability
  ≥ 2.0).  The effect is strongest in low-to-medium volatility regimes
  (`atr_q1`, `atr_q2`).

This strategy converts that statistical edge into a tradable rule.  Long
entries only — short side did not survive holdout in the same dataset.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class MomentumContinuationStrategy:
    """Long-only continuation at the 20-bar high under uptrend confirmation.

    Entry rule (at bar close)
    -------------------------
    1.  Trend filter: EMA20 > EMA50 by ≥ ``trend_strength_min`` (relative).
    2.  Proximity filter: ``high_lookback`` rolling high - close < ``near_atr``×ATR.
    3.  Volatility regime: ATR within [``min_atr_threshold``, ``max_atr_threshold``];
        0.0 disables a side of the gate.
    4.  Bar character: bullish close (close > open) — confirmation that the
        continuation is *active* this bar, not stalling.
    5.  Spread / news / session filters identical to ARB.

    Stop / target geometry
    ----------------------
    * Stop = lowest low of the prior ``stop_lookback`` bars minus
      ``stop_atr_buffer`` × ATR (gives the structure room without being too
      tight).
    * Risk-reward target derived from stop distance.

    Notes
    -----
    * Pure long-only by design — mining showed no statistically robust short
      edge in the 15-month dataset.  Adding shorts later requires its own
      separate analysis.
    * The strategy emits ``risk_reward`` so the engine recomputes the target
      from the actual fill price (eliminates the breakout-entry R:R drift
      documented in evaluation.md).
    """

    atr_period: int = 14
    ema_fast: int = 20
    ema_slow: int = 50
    high_lookback: int = 20
    stop_lookback: int = 10
    risk_reward: float = 2.0
    max_spread: float = 1.00
    near_atr: float = 0.30           # close must be within near_atr × ATR of HH
    trend_strength_min: float = 0.0005   # 5 bp EMA20/EMA50 dead-zone
    stop_atr_buffer: float = 0.5
    min_atr_threshold: float = 0.0
    max_atr_threshold: float = 0.0
    min_news_distance_minutes: float = 30.0
    allowed_sessions: tuple[str, ...] = (
        "asia", "london", "new_york",
    )
    name: str = "momentum_continuation"

    def warmup_bars(self) -> int:
        return max(self.ema_slow, self.high_lookback, self.atr_period) + 5

    # ------------------------------------------------------------------
    def signal_for(
        self, bars: Sequence[MarketBar], index: int,
    ) -> TradeSignal | None:
        if index < self.warmup_bars():
            return None
        bar = bars[index]

        # ── basic filters ──────────────────────────────────────────────
        if bar.session not in self.allowed_sessions:
            return None
        if bar.spread > self.max_spread:
            return None
        if (
            bar.news_distance_minutes is not None
            and bar.news_distance_minutes < self.min_news_distance_minutes
        ):
            return None
        if bar.close <= bar.open:
            return None  # require bullish close

        if lookback_spans_gap(bars, index, max(self.atr_period, self.high_lookback)):
            return None

        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None
        if self.min_atr_threshold > 0.0 and atr < self.min_atr_threshold:
            return None
        if self.max_atr_threshold > 0.0 and atr > self.max_atr_threshold:
            return None

        ef = self._ema(bars, index, self.ema_fast)
        es = self._ema(bars, index, self.ema_slow)
        if ef is None or es is None:
            return None
        if ef <= es * (1.0 + self.trend_strength_min):
            return None  # not in confirmed uptrend

        # Rolling high (excluding the current bar to avoid trivial self-touch).
        lo = index - self.high_lookback
        hi = index  # exclusive of current
        recent_high = max(b.high for b in bars[lo:hi])
        if recent_high - bar.close > self.near_atr * atr:
            return None  # not "near" the recent high

        # Stop = recent low minus a buffer.
        slo = index - self.stop_lookback
        recent_low = min(b.low for b in bars[slo:hi])
        stop = recent_low - self.stop_atr_buffer * atr
        # Use bar close as a proxy for assumed entry — the engine will recompute
        # the target from the actual fill via risk_reward.
        assumed_entry = bar.close
        stop_dist = assumed_entry - stop
        if stop_dist <= 0:
            return None
        # Sanity: if the stop is unreasonably wide (more than 3× ATR), the
        # structure is too messy to trust.
        if stop_dist > 3.0 * atr:
            return None

        return TradeSignal(
            side=Side.LONG,
            stop=stop,
            target=assumed_entry + stop_dist * self.risk_reward,
            reason=(
                f"Long momentum continuation: close near 20-bar high "
                f"({recent_high:.2f}), trend EMA{self.ema_fast}>EMA{self.ema_slow}, "
                f"ATR={atr:.2f}, session={bar.session}."
            ),
            tags=("momentum_continuation", bar.session),
            risk_reward=self.risk_reward,
        )

    # ------------------------------------------------------------------
    def _atr(self, bars: Sequence[MarketBar], index: int) -> float:
        start = index - self.atr_period + 1
        if start <= 0:
            return 0.0
        atr_bars = bars[start: index + 1]
        previous_close = bars[start - 1].close
        true_ranges: list[float] = []
        for b in atr_bars:
            true_ranges.append(b.true_range(previous_close))
            previous_close = b.close
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0

    def _ema(
        self, bars: Sequence[MarketBar], index: int, period: int,
    ) -> float | None:
        if index < period:
            return None
        alpha = 2.0 / (period + 1.0)
        # Seed with simple mean of first `period` closes ending at index-period+1.
        seed_end = index - period + 1
        seed = sum(b.close for b in bars[max(0, seed_end - period): seed_end]) / period
        ema = seed
        for j in range(seed_end, index + 1):
            ema = alpha * bars[j].close + (1.0 - alpha) * ema
        return ema
