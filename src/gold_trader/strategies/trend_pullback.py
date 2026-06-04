from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class TrendPullbackStrategy:
    """EMA-trend pullback continuation strategy.

    Logic
    -----
    Identifies bars where price is in a clear directional trend (EMA20 > EMA50
    by at least *trend_strength_min* × ATR) and then pulls back to test the
    EMA20.  A confirming bar — one whose wick dips at or near the EMA but whose
    close is back on the trend side — generates the entry.

    Up-trend (EMA20 > EMA50 + trend_strength_min × ATR):
        bar.low  ≤ ema20 + pullback_tolerance × ATR   (touched EMA region)
        bar.close > ema20                              (body closed above)
        → LONG;  stop = bar.low − stop_buffer × ATR

    Down-trend (EMA20 < EMA50 − trend_strength_min × ATR):
        bar.high ≥ ema20 − pullback_tolerance × ATR   (touched EMA region)
        bar.close < ema20                              (body closed below)
        → SHORT;  stop = bar.high + stop_buffer × ATR

    Gap awareness: if the ATR lookback spans a gap ≥ 4 h, signal is suppressed.
    """

    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    trend_strength_min: float = 0.8    # EMA spread / ATR must exceed this
    pullback_tolerance: float = 0.4    # how close low/high must be to EMA20 (fraction of ATR)
    stop_buffer: float = 0.15          # stop is stop_buffer × ATR beyond bar extreme
    risk_reward: float = 2.0
    max_spread: float = 0.75
    min_news_distance_minutes: float = 30.0
    allowed_sessions: tuple[str, ...] = ("london", "new_york")
    entry_slippage_buffer: float = 0.1
    name: str = "trend_pullback"

    def warmup_bars(self) -> int:
        # EMA50 needs ~3× period to stabilise
        return self.ema_slow * 3 + self.atr_period + 1

    # ------------------------------------------------------------------
    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
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
        if index < self.warmup_bars():
            return None

        if lookback_spans_gap(bars, index, self.atr_period):
            return None

        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None

        ema20 = self._ema(bars, index, self.ema_fast)
        ema50 = self._ema(bars, index, self.ema_slow)

        spread_pct_atr = (ema20 - ema50) / atr
        slippage = atr * self.entry_slippage_buffer
        tol = self.pullback_tolerance * atr
        stop_buf = self.stop_buffer * atr

        # ── Up-trend pullback ──────────────────────────────────────────
        if spread_pct_atr >= self.trend_strength_min:
            if bar.low <= ema20 + tol and bar.close > ema20:
                assumed_entry = bar.close + slippage
                stop = bar.low - stop_buf
                stop_dist = assumed_entry - stop
                if stop_dist > 0:
                    return TradeSignal(
                        side=Side.LONG,
                        stop=stop,
                        target=assumed_entry + stop_dist * self.risk_reward,
                        reason=(
                            f"Long trend-pullback to EMA{self.ema_fast} "
                            f"(trend_str={spread_pct_atr:.2f} ATR, ema20={ema20:.2f}) "
                            f"during {bar.session}."
                        ),
                        tags=("trend_pullback", "ema_touch", bar.session),
                    )

        # ── Down-trend pullback ────────────────────────────────────────
        if spread_pct_atr <= -self.trend_strength_min:
            if bar.high >= ema20 - tol and bar.close < ema20:
                assumed_entry = bar.close - slippage
                stop = bar.high + stop_buf
                stop_dist = stop - assumed_entry
                if stop_dist > 0:
                    return TradeSignal(
                        side=Side.SHORT,
                        stop=stop,
                        target=assumed_entry - stop_dist * self.risk_reward,
                        reason=(
                            f"Short trend-pullback to EMA{self.ema_fast} "
                            f"(trend_str={spread_pct_atr:.2f} ATR, ema20={ema20:.2f}) "
                            f"during {bar.session}."
                        ),
                        tags=("trend_pullback", "ema_touch", bar.session),
                    )

        return None

    # ------------------------------------------------------------------
    def _ema(self, bars: Sequence[MarketBar], index: int, period: int) -> float:
        """Exponential moving average of close over the last period × 3 bars."""
        # Use 3× length to get a well-initialised EMA
        lookback = period * 3
        start = max(0, index - lookback + 1)
        prices = [bars[i].close for i in range(start, index + 1)]
        if not prices:
            return 0.0
        multiplier = 2.0 / (period + 1)
        ema = prices[0]
        for price in prices[1:]:
            ema = price * multiplier + ema * (1.0 - multiplier)
        return ema

    # ------------------------------------------------------------------
    def _atr(self, bars: Sequence[MarketBar], index: int) -> float:
        start = index - self.atr_period + 1
        atr_bars = bars[start : index + 1]
        previous_close = bars[start - 1].close if start > 0 else None
        true_ranges: list[float] = []
        for b in atr_bars:
            true_ranges.append(b.true_range(previous_close))
            previous_close = b.close
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0
