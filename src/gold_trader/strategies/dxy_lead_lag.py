"""DXY Lead-Lag strategy.

Premise
-------
Gold (XAUUSD) is negatively correlated with the US Dollar Index (DXY).
When the DXY drops sharply during active sessions, gold typically lags the move
by 1–3 bars.  The strategy enters LONG after a significant DXY decline when
gold has not yet responded proportionally.

The ``dxy_close`` field in each ``MarketBar`` must be populated (non-None)
for this strategy to generate signals.  The recommended approach is to use
``merge-dxy`` CLI command to enrich the bars CSV with EURUSD-based DXY proxy
values before running backtests.

Signal logic
------------
For each bar i (in London or NY session):
1. Compute DXY change over *lookback* bars:
       dxy_delta = (dxy[i] - dxy[i - lookback]) / dxy[i - lookback]
2. Compute gold change over same window:
       gold_delta = (close[i] - close[i - lookback]) / close[i - lookback]
3. LONG trigger:
       dxy_delta < -min_dxy_drop  AND  gold_delta < max_gold_response × |dxy_delta|
   i.e. DXY fell more than the threshold AND gold hasn't yet responded proportionally.
4. SHORT trigger: symmetric — DXY rose > threshold AND gold hasn't caught up.
5. Stop: ATR-based away from the entry bar's close.
6. Target: entry ± risk × risk_reward.

Parameters
----------
lookback : int
    Number of bars over which the DXY and gold moves are measured (default 3).
min_dxy_drop : float
    Minimum DXY decline (as a positive fraction, default 0.002 = 0.2%)
    required to trigger a signal.
max_gold_response : float
    Maximum fraction of the DXY move that gold is allowed to have already made.
    Default 0.5 — signal fires only when gold's response is < 50% of the DXY move.
atr_period : int
    ATR smoothing window for stop sizing (default 14).
stop_atr_mult : float
    Stop distance = stop_atr_mult × ATR (default 1.0).
risk_reward : float
    Target = entry ± risk × risk_reward (default 2.0).
max_spread : float
    Skip bars with spread > max_spread (default 1.00).
min_atr_threshold : float
    Skip bars where ATR < min_atr_threshold (default 0.0 = disabled).
allowed_sessions : tuple
    Only generate signals during these sessions (default London + NY).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class DXYLeadLagStrategy:
    lookback: int = 3
    min_dxy_drop: float = 0.002          # 0.2% — minimum DXY move to trigger
    max_gold_response: float = 0.50      # gold must have responded < 50% of DXY
    atr_period: int = 14
    stop_atr_mult: float = 1.0
    risk_reward: float = 2.0
    max_spread: float = 1.00
    min_atr_threshold: float = 0.0       # disabled by default
    allowed_sessions: tuple[str, ...] = ("london", "new_york")
    name: str = "dxy_lead_lag"

    _FAMILY: ClassVar[str] = "dxy_lead_lag"

    def warmup_bars(self) -> int:
        return max(self.atr_period, self.lookback) + 2

    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        if index < self.warmup_bars():
            return None

        bar = bars[index]

        # Only active sessions.
        if bar.session not in self.allowed_sessions:
            return None

        # Spread filter.
        if bar.spread > self.max_spread:
            return None

        # dxy_close must be populated on the signal bar.
        if bar.dxy_close is None:
            return None

        # Gap-aware lookback.
        needed = max(self.atr_period, self.lookback)
        if lookback_spans_gap(bars, index, needed):
            return None

        # ATR (simple average true range).
        atr = _compute_atr(bars, index, self.atr_period)
        if atr <= 0:
            return None

        # Volatility regime filter.
        if self.min_atr_threshold > 0 and atr < self.min_atr_threshold:
            return None

        # DXY change over lookback window.
        ref_index = index - self.lookback
        ref_bar = bars[ref_index]
        if ref_bar.dxy_close is None:
            return None

        dxy_ref = ref_bar.dxy_close
        dxy_now = bar.dxy_close
        if dxy_ref <= 0:
            return None

        dxy_delta = (dxy_now - dxy_ref) / dxy_ref  # negative → DXY fell

        # Gold change over same window.
        gold_ref = ref_bar.close
        gold_now = bar.close
        if gold_ref <= 0:
            return None
        gold_delta = (gold_now - gold_ref) / gold_ref  # positive → gold rose

        # --- LONG: DXY fell significantly, gold hasn't caught up ---
        if dxy_delta <= -self.min_dxy_drop:
            # Gold response expressed relative to the magnitude of DXY move.
            # A fully-responsive gold market would show gold_delta ≈ -beta × dxy_delta.
            # We don't model beta explicitly; we just check whether gold has risen
            # by more than max_gold_response fraction of the DXY drop magnitude.
            expected_rise = abs(dxy_delta)  # symmetry assumption; no beta calibration
            actual_rise_fraction = gold_delta / expected_rise if expected_rise > 0 else 999.0
            if actual_rise_fraction < self.max_gold_response:
                stop = bar.close - self.stop_atr_mult * atr
                risk = bar.close - stop
                if risk <= 0:
                    return None
                target = bar.close + risk * self.risk_reward
                return TradeSignal(
                    side=Side.LONG,
                    stop=round(stop, 3),
                    target=round(target, 3),
                    reason=f"dxy_drop={dxy_delta:.4f} gold_resp={actual_rise_fraction:.2f}",
                    risk_reward=self.risk_reward,
                )

        # --- SHORT: DXY rose significantly, gold hasn't caught up ---
        if dxy_delta >= self.min_dxy_drop:
            expected_fall = abs(dxy_delta)
            actual_fall_fraction = (-gold_delta) / expected_fall if expected_fall > 0 else 999.0
            if actual_fall_fraction < self.max_gold_response:
                stop = bar.close + self.stop_atr_mult * atr
                risk = stop - bar.close
                if risk <= 0:
                    return None
                target = bar.close - risk * self.risk_reward
                return TradeSignal(
                    side=Side.SHORT,
                    stop=round(stop, 3),
                    target=round(target, 3),
                    reason=f"dxy_rise={dxy_delta:.4f} gold_resp={actual_fall_fraction:.2f}",
                    risk_reward=self.risk_reward,
                )

        return None


def _compute_atr(bars: Sequence[MarketBar], index: int, period: int) -> float:
    start = max(1, index - period + 1)
    total = 0.0
    count = 0
    for i in range(start, index + 1):
        prev_close = bars[i - 1].close
        total += bars[i].true_range(prev_close)
        count += 1
    return total / count if count > 0 else 0.0
