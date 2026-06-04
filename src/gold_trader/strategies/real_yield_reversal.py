"""Real-yield reversal strategy.

Thesis
------
The 10-year U.S. TIPS (real) yield is the dominant macro driver of gold across
weekly horizons.  It represents the *opportunity cost* of holding non-yielding
bullion: when real yields fall, gold's relative attractiveness rises and flows
follow with a lag of hours-to-days.  Conversely, sharp real-yield spikes drain
gold demand.

The empirical regularity (well-documented in academic and sell-side literature)
is that a multi-day real-yield move > ~10 bps tends to produce a directional
gold reaction over the next 1-3 sessions.  This strategy converts that
relationship into a discretionary-style entry gated by macro state, then
managed by structural ATR stops.

Why this is uncorrelated with ARB
---------------------------------
* ARB trigger:    intraday breakout of the Asian session range
* This trigger:   multi-day change in an external macro series
* ARB frequency:  ~1 signal/day per direction
* This frequency: ~1 signal/week (gated to first eligible bar per UTC date)
* ARB inputs:     XAUUSD bars only
* This inputs:    XAUUSD bars + DFII10 daily series

Independence is structural, not statistical-luck.

Engine contract
---------------
* Sets ``risk_reward > 0`` so the engine recomputes target from actual fill.
* Stop is ``stop_atr_mult * ATR`` away from signal-bar close.
* No use of bar.high/low for the entry decision — entry is a directional bias
  expressed at the close, executed at next bar's open by the engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone
from typing import Sequence

from ..data.macro import MacroFrame
from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class RealYieldReversalStrategy:
    """Long gold on real-yield drops, short on spikes; once-per-day cap."""

    macro: MacroFrame  # required — pass via load_macro_frame()

    yield_lookback_days: int = 5
    min_yield_move_bps: float = 10.0
    """Minimum |Δreal10y| over the lookback window, in basis points (1 bp = 0.01%)."""

    atr_period: int = 14
    stop_atr_mult: float = 1.5
    risk_reward: float = 2.0
    max_spread: float = 1.00
    min_atr_threshold: float = 0.0
    min_news_distance_minutes: float = 30.0
    allowed_sessions: tuple[str, ...] = ("london", "new_york")
    enter_longs: bool = True
    enter_shorts: bool = True
    name: str = "real_yield_reversal"

    def warmup_bars(self) -> int:
        return self.atr_period + 2

    # ------------------------------------------------------------------
    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        bar = bars[index]

        # ── Cheap filters ───────────────────────────────────────────────
        if bar.session not in self.allowed_sessions:
            return None
        if bar.spread > self.max_spread:
            return None
        if (
            bar.news_distance_minutes is not None
            and bar.news_distance_minutes < self.min_news_distance_minutes
        ):
            return None

        # ── Once-per-day gate ───────────────────────────────────────────
        # Fire only on the *first* eligible bar of each UTC calendar date so
        # the macro signal isn't spammed across all NY-session bars.
        bar_date = bar.timestamp.astimezone(timezone.utc).date()
        if index > 0:
            prev = bars[index - 1]
            prev_date = prev.timestamp.astimezone(timezone.utc).date()
            if prev_date == bar_date and prev.session in self.allowed_sessions:
                return None

        # ── Macro gate ──────────────────────────────────────────────────
        real10y = self.macro.get("real10y")
        if real10y is None:
            return None
        delta_pct = real10y.change(bar.timestamp, lookback_days=self.yield_lookback_days)
        if delta_pct is None:
            return None
        # DFII10 is published in %; 1 percentage point = 100 bps.
        delta_bps = delta_pct * 100.0

        long_signal = self.enter_longs and delta_bps <= -self.min_yield_move_bps
        short_signal = self.enter_shorts and delta_bps >= self.min_yield_move_bps
        if not (long_signal or short_signal):
            return None

        # ── ATR ─────────────────────────────────────────────────────────
        if lookback_spans_gap(bars, index, self.atr_period):
            return None
        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None
        if self.min_atr_threshold > 0.0 and atr < self.min_atr_threshold:
            return None

        stop_dist = self.stop_atr_mult * atr
        if stop_dist <= 0.0:
            return None

        # ── Build signal ────────────────────────────────────────────────
        if long_signal:
            assumed_entry = bar.close
            stop = assumed_entry - stop_dist
            return TradeSignal(
                side=Side.LONG,
                stop=stop,
                target=assumed_entry + stop_dist * self.risk_reward,
                reason=(
                    f"Real-10Y yield fell {abs(delta_bps):.1f}bps over "
                    f"{self.yield_lookback_days}d; long gold (atr={atr:.2f})."
                ),
                tags=("macro", "real_yield", "long", bar.session),
                risk_reward=self.risk_reward,
            )

        # short_signal
        assumed_entry = bar.close
        stop = assumed_entry + stop_dist
        return TradeSignal(
            side=Side.SHORT,
            stop=stop,
            target=assumed_entry - stop_dist * self.risk_reward,
            reason=(
                f"Real-10Y yield rose {delta_bps:.1f}bps over "
                f"{self.yield_lookback_days}d; short gold (atr={atr:.2f})."
            ),
            tags=("macro", "real_yield", "short", bar.session),
            risk_reward=self.risk_reward,
        )

    # ------------------------------------------------------------------
    def _atr(self, bars: Sequence[MarketBar], index: int) -> float:
        start = index - self.atr_period + 1
        if start < 0:
            return 0.0
        atr_bars = bars[start : index + 1]
        previous_close = bars[start - 1].close if start > 0 else None
        true_ranges: list[float] = []
        for b in atr_bars:
            true_ranges.append(b.true_range(previous_close))
            previous_close = b.close
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0
