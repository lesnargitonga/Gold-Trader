"""MinedPatternStrategy — convert a pattern-miner survivor into a tradable rule.

The pattern miner surfaces FDR-corrected feature conjunctions whose forward
N-bar return is statistically distinguishable from zero (e.g. ``hour_o7 &
range_q0`` long → +0.49 R over 8 bars on both 15m and 60m).

This strategy turns those conjunctions into trade rules:
- fire LONG (or SHORT) on bars where every named feature is True
- ATR-based stop at ``stop_atr × ATR`` from close
- target via ``risk_reward × stop_dist``

The whole point of this family is to test HANDBOOK lesson #2 — *do mined
forward-R edges survive stop/target conversion?* — empirically, on the
specific 5y survivors, rather than guessing.

Feature matrix and ATR are computed once per bars instance and cached
(global by ``id(bars)``) so a 16-combo grid sweep over 5y of 15m bars
takes seconds, not minutes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from ..research.features import FeatureMatrix, _atr, build_feature_matrix
from .base import lookback_spans_gap


# Cache keyed by (id(bars), len(bars)) — invalidates if bars list is mutated
# in place but is otherwise stable across param-grid sweeps that all use the
# same bars sequence.
_FM_CACHE: dict[tuple[int, int], FeatureMatrix] = {}
_ATR_CACHE: dict[tuple[int, int, int], list[float | None]] = {}


def _features_for(bars: Sequence[MarketBar]) -> FeatureMatrix:
    key = (id(bars), len(bars))
    fm = _FM_CACHE.get(key)
    if fm is None:
        fm = build_feature_matrix(bars)
        _FM_CACHE[key] = fm
    return fm


def _atr_for(bars: Sequence[MarketBar], period: int) -> list[float | None]:
    key = (id(bars), len(bars), period)
    series = _ATR_CACHE.get(key)
    if series is None:
        series = _atr(bars, period)
        _ATR_CACHE[key] = series
    return series


@dataclass(frozen=True)
class MinedPatternStrategy:
    """Generic conjunction-of-features → directional ATR-stop trade.

    Parameters
    ----------
    feature_names
        Tuple of feature names (must exist in the 69-feature base vocabulary).
    direction
        ``"long"`` or ``"short"``.
    atr_period
        ATR window for stop sizing.
    stop_atr
        Stop distance in ATR multiples (from bar close).
    risk_reward
        Target distance = ``risk_reward × stop_dist``.  Set > 0 so the
        engine's ``risk_reward`` quirk recomputes the target from the
        actual fill, preserving R:R despite next-bar-open drift.
    max_spread
        Skip the bar if reported spread exceeds this.
    """

    feature_names: tuple[str, ...] = ()
    direction: str = "long"
    atr_period: int = 14
    stop_atr: float = 1.0
    risk_reward: float = 2.0
    max_spread: float = 1.0
    name: str = "mined_pattern"

    def warmup_bars(self) -> int:
        # Feature percentile windows use ~200 bars; ATR needs `period+1`.
        return max(self.atr_period + 1, 250)

    def signal_for(
        self, bars: Sequence[MarketBar], index: int
    ) -> TradeSignal | None:
        if not self.feature_names:
            return None
        if index < self.warmup_bars():
            return None
        if lookback_spans_gap(bars, index, self.atr_period):
            return None
        bar = bars[index]
        if bar.spread > self.max_spread:
            return None

        fm = _features_for(bars)
        for fname in self.feature_names:
            vec = fm.features.get(fname)
            if vec is None:
                return None  # feature not in vocab — silent miss
            if not vec[index]:
                return None

        atr = _atr_for(bars, self.atr_period)[index]
        if atr is None or atr <= 0.0:
            return None

        stop_dist = self.stop_atr * atr
        if stop_dist <= 0.0:
            return None

        if self.direction == "long":
            side = Side.LONG
            stop = bar.close - stop_dist
            target = bar.close + stop_dist * self.risk_reward
        elif self.direction == "short":
            side = Side.SHORT
            stop = bar.close + stop_dist
            target = bar.close - stop_dist * self.risk_reward
        else:
            return None

        return TradeSignal(
            side=side,
            stop=stop,
            target=target,
            reason=(
                f"mined pattern: {' & '.join(self.feature_names)} "
                f"-> {self.direction} (atr={atr:.3f})"
            ),
            tags=("mined",) + tuple(self.feature_names),
            risk_reward=self.risk_reward,
        )
