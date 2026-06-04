"""Regime detection layer.

Single-source-of-truth classifier of *what kind of market we're in right now*.

The honest-eval feedback was clear: the same entry logic produces wildly
different expectancy across regimes, and the project's biggest leverage
is a regime *filter*, not yet-another entry.

This module returns a deterministic dict of regime tags computed from
both the bar series (vol percentile, trend state, spread regime,
compression vs expansion) and (optionally) a MacroFrame (real-yield
direction, DXY trend, VIX state).  No lookahead — every value is
computed strictly from data at-or-before the index.

Usage::

    detector = RegimeDetector()
    tags = detector.classify(bars, index=i, macro=macro_frame)
    if tags["vol_pct"] == "high" and tags["macro_real10y"] == "falling":
        # supportive regime
        ...

The returned dict is a flat mapping from category -> token, which makes
it trivial to serialize, log, tag trades with, or feed into a filter.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .data.macro import MacroFrame
from .models import MarketBar


@dataclass(frozen=True)
class RegimeTags:
    """Container with both dict access and named slots."""

    vol_pct: str        # "low" | "mid" | "high"
    trend: str          # "up" | "flat" | "down"
    compression: str    # "expanding" | "stable" | "compressing"
    spread: str         # "tight" | "normal" | "wide"
    macro_real10y: str  # "falling" | "flat" | "rising" | "unknown"
    macro_dxy: str      # "weak" | "flat" | "strong" | "unknown"
    macro_vix: str      # "calm" | "elevated" | "stressed" | "unknown"
    macro_stagflation: bool  # bei10 hi + real10y lo
    session_vwap: str   # "above" | "at" | "below" — close vs session VWAP

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "vol_pct": self.vol_pct,
            "trend": self.trend,
            "compression": self.compression,
            "spread": self.spread,
            "macro_real10y": self.macro_real10y,
            "macro_dxy": self.macro_dxy,
            "macro_vix": self.macro_vix,
            "macro_stagflation": self.macro_stagflation,
            "session_vwap": self.session_vwap,
        }


@dataclass(frozen=True)
class RegimeDetector:
    """Pure regime classifier.  No state — call ``classify`` with bars/macro."""

    atr_period: int = 14
    vol_lookback_bars: int = 200          # rolling window for vol percentile
    trend_fast_period: int = 20
    trend_slow_period: int = 50
    compression_lookback: int = 20
    spread_lookback_bars: int = 200
    macro_lookback_days: int = 5
    macro_dxy_lookback_days: int = 20
    real_yield_flat_threshold_bps: float = 3.0
    dxy_flat_threshold_pct: float = 0.5
    vix_calm_max: float = 18.0
    vix_stressed_min: float = 28.0

    def classify(
        self,
        bars: Sequence[MarketBar],
        index: int,
        macro: MacroFrame | None = None,
    ) -> RegimeTags:
        if index < 1 or index >= len(bars):
            raise IndexError(f"index {index} out of range for {len(bars)} bars")

        vol_pct = self._vol_percentile(bars, index)
        trend = self._trend_state(bars, index)
        compression = self._compression_state(bars, index)
        spread = self._spread_state(bars, index)
        session_vwap = self._session_vwap_state(bars, index)

        macro_real = "unknown"
        macro_dxy = "unknown"
        macro_vix = "unknown"
        stagflation = False
        if macro is not None:
            ts = bars[index].timestamp
            real = macro.get("real10y")
            if real is not None:
                d = real.change(ts, lookback_days=self.macro_lookback_days)
                if d is not None:
                    bps = d * 100.0
                    if bps > self.real_yield_flat_threshold_bps:
                        macro_real = "rising"
                    elif bps < -self.real_yield_flat_threshold_bps:
                        macro_real = "falling"
                    else:
                        macro_real = "flat"
            dxy = macro.get("dxy")
            if dxy is not None:
                d = dxy.pct_change(ts, lookback_days=self.macro_dxy_lookback_days)
                if d is not None:
                    pct = d * 100.0
                    if pct > self.dxy_flat_threshold_pct:
                        macro_dxy = "strong"
                    elif pct < -self.dxy_flat_threshold_pct:
                        macro_dxy = "weak"
                    else:
                        macro_dxy = "flat"
            vix = macro.get("vix")
            if vix is not None:
                v = vix.as_of(ts)
                if v is not None:
                    if v <= self.vix_calm_max:
                        macro_vix = "calm"
                    elif v >= self.vix_stressed_min:
                        macro_vix = "stressed"
                    else:
                        macro_vix = "elevated"
            bei = macro.get("bei10")
            if bei is not None and real is not None:
                bv = bei.as_of(ts)
                rv = real.as_of(ts)
                if bv is not None and rv is not None:
                    bei_vals = sorted(p.value for p in bei.points)
                    real_vals = sorted(p.value for p in real.points)
                    if len(bei_vals) >= 9 and len(real_vals) >= 9:
                        bei_hi = bei_vals[(2 * len(bei_vals)) // 3]
                        real_lo = real_vals[len(real_vals) // 3]
                        if bv >= bei_hi and rv <= real_lo:
                            stagflation = True

        return RegimeTags(
            vol_pct=vol_pct,
            trend=trend,
            compression=compression,
            spread=spread,
            macro_real10y=macro_real,
            macro_dxy=macro_dxy,
            macro_vix=macro_vix,
            macro_stagflation=stagflation,
            session_vwap=session_vwap,
        )

    # ------------------------------------------------------------------
    # Bar-side regime computations
    # ------------------------------------------------------------------

    def _vol_percentile(self, bars: Sequence[MarketBar], index: int) -> str:
        atr_now = self._atr_at(bars, index)
        if atr_now is None:
            return "mid"
        start = max(0, index - self.vol_lookback_bars)
        window: list[float] = []
        for j in range(start, index + 1):
            v = self._atr_at(bars, j)
            if v is not None:
                window.append(v)
        if len(window) < 30:
            return "mid"
        window.sort()
        # rank of atr_now in window
        below = sum(1 for v in window if v < atr_now)
        pct = below / len(window)
        if pct < 0.33:
            return "low"
        if pct > 0.66:
            return "high"
        return "mid"

    def _atr_at(self, bars: Sequence[MarketBar], index: int) -> float | None:
        # Wilder ATR, computed at index using last `atr_period` bars.
        if index < self.atr_period:
            return None
        trs: list[float] = []
        for j in range(index - self.atr_period + 1, index + 1):
            if j == 0:
                trs.append(bars[0].high - bars[0].low)
                continue
            prev_close = bars[j - 1].close
            tr = max(
                bars[j].high - bars[j].low,
                abs(bars[j].high - prev_close),
                abs(bars[j].low - prev_close),
            )
            trs.append(tr)
        return sum(trs) / len(trs)

    def _ema_at(
        self, bars: Sequence[MarketBar], index: int, period: int,
    ) -> float | None:
        if index < period - 1:
            return None
        alpha = 2.0 / (period + 1)
        ema = bars[index - period + 1].close
        for j in range(index - period + 2, index + 1):
            ema = alpha * bars[j].close + (1 - alpha) * ema
        return ema

    def _trend_state(self, bars: Sequence[MarketBar], index: int) -> str:
        fast = self._ema_at(bars, index, self.trend_fast_period)
        slow = self._ema_at(bars, index, self.trend_slow_period)
        if fast is None or slow is None:
            return "flat"
        gap = (fast - slow) / slow
        if gap > 0.002:
            return "up"
        if gap < -0.002:
            return "down"
        return "flat"

    def _compression_state(self, bars: Sequence[MarketBar], index: int) -> str:
        if index < self.compression_lookback * 2:
            return "stable"
        recent = bars[index - self.compression_lookback + 1 : index + 1]
        prior = bars[
            index - 2 * self.compression_lookback + 1 : index - self.compression_lookback + 1
        ]
        recent_range = max(b.high for b in recent) - min(b.low for b in recent)
        prior_range = max(b.high for b in prior) - min(b.low for b in prior)
        if prior_range <= 0:
            return "stable"
        ratio = recent_range / prior_range
        if ratio > 1.25:
            return "expanding"
        if ratio < 0.8:
            return "compressing"
        return "stable"

    def _spread_state(self, bars: Sequence[MarketBar], index: int) -> str:
        start = max(0, index - self.spread_lookback_bars)
        window = [bars[j].spread for j in range(start, index + 1) if bars[j].spread > 0]
        if len(window) < 20:
            return "normal"
        cur = bars[index].spread
        window_sorted = sorted(window)
        below = sum(1 for v in window_sorted if v < cur)
        pct = below / len(window_sorted)
        if pct < 0.33:
            return "tight"
        if pct > 0.66:
            return "wide"
        return "normal"

    def _session_vwap_state(self, bars: Sequence[MarketBar], index: int) -> str:
        """Daily-session VWAP, reset at UTC midnight.

        Volume-less bars (volume==0) fall back to a typical-price arithmetic
        mean — still a useful intraday reference even without true volume.
        Threshold: 0.05% (5bps) band around VWAP qualifies as 'at'.
        """
        if index < 1:
            return "at"
        anchor_date = bars[index].timestamp.date()
        # Walk back to first bar of this UTC day.
        start = index
        while start > 0 and bars[start - 1].timestamp.date() == anchor_date:
            start -= 1
        num = 0.0
        den = 0.0
        for j in range(start, index + 1):
            b = bars[j]
            tp = (b.high + b.low + b.close) / 3.0
            v = b.volume if b.volume and b.volume > 0 else 1.0
            num += tp * v
            den += v
        if den <= 0:
            return "at"
        vwap = num / den
        close = bars[index].close
        if vwap <= 0:
            return "at"
        diff = (close - vwap) / vwap
        if diff > 0.0005:
            return "above"
        if diff < -0.0005:
            return "below"
        return "at"
