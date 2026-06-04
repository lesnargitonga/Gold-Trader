"""Multi-timeframe-aware strategy wrappers and HTF-native strategies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..backtest.mtf_context import MTFContext
from ..models import MarketBar, Side, TradeSignal
from .base import Strategy, lookback_spans_gap


# ---------------------------------------------------------------------------
# Generic HTF gating wrapper
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HTFTrendGate:
    """Wrap any single-TF :class:`Strategy` and pass-through only those
    signals whose ``side`` agrees with the higher-timeframe trend.

    Modes
    -----
    - ``follow``: LONG only when HTF trend == "up", SHORT only when "down"
    - ``fade``:   accept only when HTF trend == "flat" (range conditions)
    - ``with_or_flat``: accept "up"+LONG, "down"+SHORT, OR "flat" either side
    """

    inner: Strategy
    htf: str
    mode: str = "follow"  # 'follow' | 'fade' | 'with_or_flat'

    @property
    def name(self) -> str:
        return f"{self.inner.name}_htf{self.htf}_{self.mode}"

    def warmup_bars(self) -> int:
        return self.inner.warmup_bars()

    def required_htf(self) -> tuple[str, ...]:
        return (self.htf,)

    def signal_for_mtf(
        self,
        bars: Sequence[MarketBar],
        index: int,
        mtf: MTFContext,
    ) -> TradeSignal | None:
        sig = self.inner.signal_for(bars, index)
        if sig is None:
            return None
        trend = mtf.trend(self.htf)
        if self.mode == "follow":
            if sig.side is Side.LONG and trend == "up":
                return sig
            if sig.side is Side.SHORT and trend == "down":
                return sig
            return None
        if self.mode == "fade":
            return sig if trend == "flat" else None
        if self.mode == "with_or_flat":
            if trend == "flat":
                return sig
            if sig.side is Side.LONG and trend == "up":
                return sig
            if sig.side is Side.SHORT and trend == "down":
                return sig
            return None
        return sig  # unknown mode: pass-through (safe default)


# ---------------------------------------------------------------------------
# Native HTF strategies
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HTFTrendPullback:
    """HTF-native pullback-to-EMA strategy.

    Designed for 60m or 240m primary timeframe.  Logic:

    1. Querying the higher-still timeframe (``align_tf``), require
       trend == LONG (up) or SHORT (down).  This becomes the only
       trade direction.
    2. On the PRIMARY timeframe, after a pullback that touches a fast
       EMA (close pulls in toward the EMA from the trend side), enter
       on the next bar.

    The fast EMA on the primary is computed via the indicator cache on
    primary bars.  Stop = swing low/high of the last ``swing_lookback``
    bars; target = ``risk_reward`` × stop distance.
    """

    align_tf: str = "240m"          # the HTF whose trend gates direction
    fast_ema_period: int = 20        # primary-side fast EMA
    swing_lookback: int = 10
    risk_reward: float = 2.0
    name: str = "htf_trend_pullback"

    def warmup_bars(self) -> int:
        return max(self.fast_ema_period * 3, self.swing_lookback) + 2

    def required_htf(self) -> tuple[str, ...]:
        return (self.align_tf,)

    def signal_for_mtf(
        self,
        bars: Sequence[MarketBar],
        index: int,
        mtf: MTFContext,
    ) -> TradeSignal | None:
        if index < self.warmup_bars() or index >= len(bars) - 1:
            return None

        trend = mtf.trend(self.align_tf)
        if trend == "flat":
            return None

        if lookback_spans_gap(bars, index, self.swing_lookback):
            return None

        # Compute primary EMA fast on the fly (cheap O(period))
        ema = bars[index - self.fast_ema_period * 2].close
        alpha = 2 / (self.fast_ema_period + 1)
        for j in range(index - self.fast_ema_period * 2 + 1, index + 1):
            ema = alpha * bars[j].close + (1 - alpha) * ema

        bar = bars[index]
        prev = bars[index - 1]

        if trend == "up":
            # Pullback: prior bar low touched-or-pierced EMA, current bar
            # closes back above EMA (rejection).
            if prev.low <= ema and bar.close > ema and bar.close > bar.open:
                lookback = bars[max(0, index - self.swing_lookback): index + 1]
                stop = min(b.low for b in lookback)
                if stop >= bar.close:
                    return None
                stop_dist = bar.close - stop
                return TradeSignal(
                    side=Side.LONG,
                    stop=stop,
                    target=bar.close + stop_dist * self.risk_reward,
                    reason=f"htf_pullback_long ema={ema:.2f} stop={stop:.2f}",
                    tags=("mtf", "pullback", "long"),
                    risk_reward=self.risk_reward,
                )
        elif trend == "down":
            if prev.high >= ema and bar.close < ema and bar.close < bar.open:
                lookback = bars[max(0, index - self.swing_lookback): index + 1]
                stop = max(b.high for b in lookback)
                if stop <= bar.close:
                    return None
                stop_dist = stop - bar.close
                return TradeSignal(
                    side=Side.SHORT,
                    stop=stop,
                    target=bar.close - stop_dist * self.risk_reward,
                    reason=f"htf_pullback_short ema={ema:.2f} stop={stop:.2f}",
                    tags=("mtf", "pullback", "short"),
                    risk_reward=self.risk_reward,
                )
        return None


@dataclass(frozen=True)
class HTFBreakoutContinuation:
    """HTF-native momentum breakout continuation.

    Logic:
    1. HTF trend (``align_tf``) must be up or down.
    2. On primary, current bar must close above the highest high of the
       previous ``range_lookback`` bars (LONG) or below the lowest low
       (SHORT) AND be aligned with HTF trend.
    3. Stop placed at the opposite end of the range; target = RR ×
       stop distance.
    """

    align_tf: str = "240m"
    range_lookback: int = 12
    risk_reward: float = 2.0
    name: str = "htf_breakout_continuation"

    def warmup_bars(self) -> int:
        return self.range_lookback + 2

    def required_htf(self) -> tuple[str, ...]:
        return (self.align_tf,)

    def signal_for_mtf(
        self,
        bars: Sequence[MarketBar],
        index: int,
        mtf: MTFContext,
    ) -> TradeSignal | None:
        if index < self.warmup_bars() or index >= len(bars) - 1:
            return None
        trend = mtf.trend(self.align_tf)
        if trend == "flat":
            return None
        if lookback_spans_gap(bars, index, self.range_lookback):
            return None
        bar = bars[index]
        window = bars[index - self.range_lookback: index]
        hi = max(b.high for b in window)
        lo = min(b.low for b in window)

        if trend == "up" and bar.close > hi:
            stop = lo
            stop_dist = bar.close - stop
            if stop_dist <= 0:
                return None
            return TradeSignal(
                side=Side.LONG,
                stop=stop,
                target=bar.close + stop_dist * self.risk_reward,
                reason=f"htf_break_long hi={hi:.2f} lo={lo:.2f}",
                tags=("mtf", "breakout", "long"),
                risk_reward=self.risk_reward,
            )
        if trend == "down" and bar.close < lo:
            stop = hi
            stop_dist = stop - bar.close
            if stop_dist <= 0:
                return None
            return TradeSignal(
                side=Side.SHORT,
                stop=stop,
                target=bar.close - stop_dist * self.risk_reward,
                reason=f"htf_break_short hi={hi:.2f} lo={lo:.2f}",
                tags=("mtf", "breakout", "short"),
                risk_reward=self.risk_reward,
            )
        return None


# ---------------------------------------------------------------------------
# Regime-aware wrapper
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegimeGatedMTF:
    """Gate any MTFStrategy with HTF trend-strength + ATR-percentile checks.

    Two filters, both evaluated on ``align_tf``:

    1. **Trend strength**: ``|ema_fast - ema_slow| / atr >= min_trend_strength_atr``
       — rejects weak / overlapping EMAs (chop).

    2. **ATR percentile band**: rolling-window percentile of the current
       HTF ATR must be inside ``[atr_pct_low, atr_pct_high]`` — rejects
       both dead-flat and blow-off vol regimes.

    Either filter can be disabled by setting the threshold to a value
    that always passes (``min_trend_strength_atr=0`` or
    ``atr_pct_low=0, atr_pct_high=1``).
    """

    inner: object  # MTFStrategy
    align_tf: str = "240m"
    min_trend_strength_atr: float = 0.5
    atr_pct_window: int = 100
    atr_pct_low: float = 0.10
    atr_pct_high: float = 0.95

    @property
    def name(self) -> str:
        return f"{getattr(self.inner, 'name', 'inner')}_regime"

    def warmup_bars(self) -> int:
        return self.inner.warmup_bars()

    def required_htf(self) -> tuple[str, ...]:
        base = tuple(self.inner.required_htf())
        return base if self.align_tf in base else base + (self.align_tf,)

    def signal_for_mtf(
        self,
        bars: Sequence[MarketBar],
        index: int,
        mtf: MTFContext,
    ) -> TradeSignal | None:
        sig = self.inner.signal_for_mtf(bars, index, mtf)
        if sig is None:
            return None
        idx = mtf.htf_index(self.align_tf)
        if idx < 0:
            return None
        cache = mtf.indicators.get(self.align_tf)
        if cache is None:
            return None
        ef = cache.ema_fast_at(idx)
        es = cache.ema_slow_at(idx)
        atr = cache.atr_at(idx)
        if ef is None or es is None or atr is None or atr <= 0:
            return None

        strength = abs(ef - es) / atr
        if strength < self.min_trend_strength_atr:
            return None

        lo = max(0, idx - self.atr_pct_window + 1)
        window = [a for a in cache.atr[lo: idx + 1] if a is not None and a > 0]
        if len(window) < 10:
            return None
        rank = sum(1 for a in window if a <= atr) / len(window)
        if rank < self.atr_pct_low or rank > self.atr_pct_high:
            return None

        return sig


__all__ = [
    "HTFTrendGate",
    "HTFTrendPullback",
    "HTFBreakoutContinuation",
    "RegimeGatedMTF",
]
