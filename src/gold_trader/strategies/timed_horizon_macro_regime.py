"""TimedHorizonMacroRegimeStrategy.

Designed to capture the *exact* statistical surface measured by the
pattern miner: enter on a regime conjunction at bar close and **exit
after a fixed number of bars** regardless of price.  Stop and target
are placed so far away (default ±10×ATR) that they almost never
trigger, leaving the engine's ``max_hold_bars`` to drive the exit.

This validates whether the miner's avg_R signal is real-tradable, or
whether stop/target conversion is what was killing the edge in the
earlier `MacroRegimeContinuationStrategy`.

Long-only — short side has no validated edge in this dataset.

Usage requires running the backtest with
``BacktestConfig(max_hold_bars=N)`` set to the same horizon the miner
used (e.g. 16 bars on 60m = the 16-bar horizon that produced avg_R
+1.89 in the macro sweep).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Sequence

from ..data.macro import MacroFrame
from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class TimedHorizonMacroRegimeStrategy:
    """Long gold under bullish macro regime, time-exit only."""

    macro: MacroFrame  # required

    # Regime gate ---------------------------------------------------------
    real_yield_lookback_days: int = 5
    real_yield_max_change_bps: float = 0.0
    """real10y must fall by at least this many bps (negative = drop)."""

    vix_lookback_days: int = 5
    vix_max_change_abs: float = 1.50

    require_dxy_flat: bool = True
    dxy_lookback_days: int = 20
    dxy_max_abs_change_pct: float = 1.00

    # Engine hooks --------------------------------------------------------
    atr_period: int = 14
    far_atr_mult: float = 10.0
    """Stop / target distance in ATRs.  Set wide enough that they almost
    never trigger; the engine's max_hold_bars handles the exit."""

    once_per_day: bool = True
    require_bullish_close: bool = False
    max_spread: float = 1.50
    min_news_distance_minutes: float = 30.0
    allowed_sessions: tuple[str, ...] = ("london", "new_york")
    name: str = "timed_horizon_macro_regime"

    def warmup_bars(self) -> int:
        return self.atr_period + 5

    # ------------------------------------------------------------------
    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        if index < self.warmup_bars():
            return None
        bar = bars[index]

        if bar.session not in self.allowed_sessions:
            return None
        if bar.spread > self.max_spread:
            return None
        if (
            bar.news_distance_minutes is not None
            and bar.news_distance_minutes < self.min_news_distance_minutes
        ):
            return None
        if self.require_bullish_close and bar.close <= bar.open:
            return None

        if self.once_per_day and index > 0:
            today = bar.timestamp.astimezone(timezone.utc).date()
            prev = bars[index - 1]
            prev_d = prev.timestamp.astimezone(timezone.utc).date()
            if prev_d == today and prev.session in self.allowed_sessions:
                return None

        # Macro gate.
        if not self._regime_ok(bar):
            return None

        # ATR for far stops.
        if lookback_spans_gap(bars, index, self.atr_period):
            return None
        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None

        entry = bar.close
        far = self.far_atr_mult * atr
        return TradeSignal(
            side=Side.LONG,
            stop=entry - far,
            target=entry + far,
            reason=(
                f"Timed-horizon macro long: regime favourable, "
                f"atr={atr:.2f}, exit on max_hold_bars."
            ),
            tags=("macro", "timed_horizon", "long", bar.session),
            risk_reward=0.0,  # do NOT recompute target — keep wide
        )

    # ------------------------------------------------------------------
    def _regime_ok(self, bar: MarketBar) -> bool:
        ts = bar.timestamp
        m = self.macro

        real = m.get("real10y")
        if real is None:
            return False
        d_real = real.change(ts, lookback_days=self.real_yield_lookback_days)
        if d_real is None:
            return False
        if d_real * 100.0 > self.real_yield_max_change_bps:
            return False

        vix = m.get("vix")
        if vix is None:
            return False
        d_vix = vix.change(ts, lookback_days=self.vix_lookback_days)
        if d_vix is None:
            return False
        if abs(d_vix) > self.vix_max_change_abs:
            return False

        if self.require_dxy_flat:
            dxy = m.get("dxy")
            if dxy is None:
                return False
            d_dxy = dxy.pct_change(ts, lookback_days=self.dxy_lookback_days)
            if d_dxy is None:
                return False
            if abs(d_dxy * 100.0) > self.dxy_max_abs_change_pct:
                return False
        return True

    # ------------------------------------------------------------------
    def _atr(self, bars: Sequence[MarketBar], index: int) -> float:
        start = index - self.atr_period + 1
        if start <= 0:
            return 0.0
        atr_bars = bars[start: index + 1]
        previous_close = bars[start - 1].close
        trs: list[float] = []
        for b in atr_bars:
            trs.append(b.true_range(previous_close))
            previous_close = b.close
        return sum(trs) / len(trs) if trs else 0.0
