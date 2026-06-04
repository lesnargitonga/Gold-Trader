"""Higher-timeframe indicator cache.

Pre-computes EMA, ATR, swing structure, and a coarse trend/regime label
for every bar of an HTF series.  Indicators are computed once per series
load and queried in O(1) by HTF index.

All indicators respect the strict no-look-ahead invariant of
:class:`gold_trader.data.mtf.MTFBundle`: caller must use the HTF index
returned by ``MTFBundle.htf_index_at`` (the latest *closed* bar).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import MarketBar


# ---------------------------------------------------------------------------
# Primitive series
# ---------------------------------------------------------------------------

def ema_series(values: Sequence[float], period: int) -> list[float]:
    """Standard EMA seeded with first value.  ``period`` must be >= 1."""
    if period < 1:
        raise ValueError("period must be >= 1")
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def true_range_series(bars: Sequence[MarketBar]) -> list[float]:
    out: list[float] = []
    prev_close: float | None = None
    for b in bars:
        out.append(b.true_range(prev_close))
        prev_close = b.close
    return out


def atr_series(bars: Sequence[MarketBar], period: int = 14) -> list[float]:
    """Wilder-style ATR.  Output[i] is ATR using bars[..i] inclusive."""
    if period < 1:
        raise ValueError("period must be >= 1")
    tr = true_range_series(bars)
    out: list[float] = []
    if not bars:
        return out
    # Seed with simple average of first `period` true ranges; before that,
    # fall back to running mean.
    running = 0.0
    for i, t in enumerate(tr):
        if i < period:
            running += t
            out.append(running / (i + 1))
        else:
            prev = out[-1]
            out.append((prev * (period - 1) + t) / period)
    return out


# ---------------------------------------------------------------------------
# Swing structure
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SwingPoint:
    index: int
    price: float
    kind: str  # "high" or "low"


def swing_points(bars: Sequence[MarketBar], left: int = 2, right: int = 2) -> list[SwingPoint]:
    """Identify confirmed swing highs/lows using a left/right pivot rule.

    A bar at index ``i`` is a swing high if its high is strictly greater
    than every high in ``[i-left, i+right]`` (excluding i itself).  Pivots
    are confirmed only after ``right`` further bars have closed; this is
    accounted for naturally because we iterate over the full series.

    For *online* use during backtesting, a swing is "known" only at index
    ``i + right`` — see :func:`structure_at`.
    """
    out: list[SwingPoint] = []
    n = len(bars)
    for i in range(left, n - right):
        h = bars[i].high
        l = bars[i].low
        is_high = all(bars[j].high < h for j in range(i - left, i)) and \
                  all(bars[j].high < h for j in range(i + 1, i + right + 1))
        is_low = all(bars[j].low > l for j in range(i - left, i)) and \
                 all(bars[j].low > l for j in range(i + 1, i + right + 1))
        if is_high:
            out.append(SwingPoint(index=i, price=h, kind="high"))
        if is_low:
            out.append(SwingPoint(index=i, price=l, kind="low"))
    return out


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HTFIndicatorCache:
    """Pre-computed indicators for one HTF bar series.

    All series are aligned to the bar series — ``ema_fast[j]`` is the EMA
    using bars ``[0..j]`` inclusive.
    """

    ema_fast: tuple[float, ...]
    ema_slow: tuple[float, ...]
    atr: tuple[float, ...]
    # Confirmed swings (using left/right pivot rule).  Each entry holds
    # the bar index at which it was *confirmed* (i.e. swing.index + right).
    swings: tuple[SwingPoint, ...]
    # For O(1) trend lookups: precomputed at every bar.
    # 'up'   : close > ema_slow AND ema_slow rising over ``trend_lookback`` bars
    # 'down' : close < ema_slow AND ema_slow falling
    # 'flat' : neither
    trend: tuple[str, ...]

    fast_period: int
    slow_period: int
    atr_period: int
    swing_left: int
    swing_right: int
    trend_lookback: int

    def trend_at(self, idx: int) -> str:
        if idx < 0 or idx >= len(self.trend):
            return "flat"
        return self.trend[idx]

    def ema_fast_at(self, idx: int) -> float | None:
        if idx < 0 or idx >= len(self.ema_fast):
            return None
        return self.ema_fast[idx]

    def ema_slow_at(self, idx: int) -> float | None:
        if idx < 0 or idx >= len(self.ema_slow):
            return None
        return self.ema_slow[idx]

    def atr_at(self, idx: int) -> float | None:
        if idx < 0 or idx >= len(self.atr):
            return None
        return self.atr[idx]

    def last_confirmed_swing_high(self, up_to_idx: int) -> SwingPoint | None:
        """Return most recent swing-high *confirmed* at-or-before ``up_to_idx``.

        A swing at bar ``i`` (left/right=L/R) is confirmed at bar
        ``i + R``.  We treat the confirmation index as the earliest the
        swing is usable — preserves no-look-ahead.
        """
        result: SwingPoint | None = None
        for sw in self.swings:
            if sw.kind != "high":
                continue
            confirm_idx = sw.index + self.swing_right
            if confirm_idx > up_to_idx:
                break
            result = sw
        return result

    def last_confirmed_swing_low(self, up_to_idx: int) -> SwingPoint | None:
        result: SwingPoint | None = None
        for sw in self.swings:
            if sw.kind != "low":
                continue
            confirm_idx = sw.index + self.swing_right
            if confirm_idx > up_to_idx:
                break
            result = sw
        return result


def build_indicator_cache(
    bars: Sequence[MarketBar],
    *,
    fast_period: int = 20,
    slow_period: int = 50,
    atr_period: int = 14,
    swing_left: int = 2,
    swing_right: int = 2,
    trend_lookback: int = 5,
) -> HTFIndicatorCache:
    closes = [b.close for b in bars]
    ema_f = ema_series(closes, fast_period)
    ema_s = ema_series(closes, slow_period)
    atr = atr_series(bars, atr_period)
    swings = swing_points(bars, left=swing_left, right=swing_right)

    trend: list[str] = []
    for i in range(len(bars)):
        if i < max(slow_period, trend_lookback):
            trend.append("flat")
            continue
        c = closes[i]
        e = ema_s[i]
        e_prev = ema_s[i - trend_lookback]
        if c > e and e > e_prev:
            trend.append("up")
        elif c < e and e < e_prev:
            trend.append("down")
        else:
            trend.append("flat")

    return HTFIndicatorCache(
        ema_fast=tuple(ema_f),
        ema_slow=tuple(ema_s),
        atr=tuple(atr),
        swings=tuple(swings),
        trend=tuple(trend),
        fast_period=fast_period,
        slow_period=slow_period,
        atr_period=atr_period,
        swing_left=swing_left,
        swing_right=swing_right,
        trend_lookback=trend_lookback,
    )


__all__ = [
    "ema_series",
    "true_range_series",
    "atr_series",
    "SwingPoint",
    "swing_points",
    "HTFIndicatorCache",
    "build_indicator_cache",
]
