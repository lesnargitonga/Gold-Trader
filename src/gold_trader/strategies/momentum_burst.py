from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class MomentumBurstStrategy:
    """Single-bar momentum burst continuation strategy.

    Logic
    -----
    Identifies bars with exceptionally large directional bodies — a sign of
    concentrated institutional order flow — then enters a continuation trade
    on the *next* bar in the same direction.

    Signal bar criteria:
      - Body size (|close − open|) ≥ *min_body_atr* × ATR (strong conviction)
      - Close in the top *body_fraction* of bar range (long) or bottom (short):
          long : (close − low) / (high − low) ≥ 1 − *body_fraction*
          short: (high − close) / (high − low) ≥ 1 − *body_fraction*
      - Spread ≤ max_spread on the signal bar
      - Session filter: london or new_york
      - MACD histogram > 0 (long) or < 0 (short) — momentum in right direction

    Entry is taken at the *next* bar's open (bar[i+1]) as usual; the signal
    is generated at bar i.  ``assumed_entry = bar.close ± slippage_buffer×ATR``
    gives the pre-signal best estimate.

    Stop:   burst bar's opposite extreme (low for long, high for short)
    Target: entry ± risk_reward × stop_dist

    Gap awareness: ATR gap-check via shared helper.  Signal bars spanning
    gaps are suppressed.
    """

    atr_period: int = 14
    min_body_atr: float = 0.60      # body must be ≥ 60% × ATR to count as a burst
    body_fraction: float = 0.25     # close must be in top/bottom 25% of bar range
    risk_reward: float = 2.0
    max_spread: float = 0.75
    min_news_distance_minutes: float = 30.0
    allowed_sessions: tuple[str, ...] = ("london", "new_york")
    entry_slippage_buffer: float = 0.1
    min_atr_threshold: float = 0.0  # volatility regime filter (0 = disabled)
    name: str = "momentum_burst"

    # How many bars of MACD to look back for histogram sign
    _macd_fast: int = 12
    _macd_slow: int = 26
    _macd_signal: int = 9

    def warmup_bars(self) -> int:
        return max(self.atr_period, self._macd_slow + self._macd_signal) + 5

    # ------------------------------------------------------------------
    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
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
        if index < self.warmup_bars():
            return None

        if lookback_spans_gap(bars, index, self.atr_period):
            return None

        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None
        if self.min_atr_threshold > 0.0 and atr < self.min_atr_threshold:
            return None

        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return None

        body = abs(bar.close - bar.open)
        if body < self.min_body_atr * atr:
            return None   # not a strong enough burst

        slippage = self.entry_slippage_buffer * atr
        macd_hist = self._macd_histogram(bars, index)

        # ── Long: large bullish body, close in top fraction, MACD+ ────
        close_position = (bar.close - bar.low) / bar_range
        if bar.close > bar.open and close_position >= (1.0 - self.body_fraction):
            if macd_hist >= 0:
                assumed_entry = bar.close + slippage
                stop = bar.low
                stop_dist = assumed_entry - stop
                if stop_dist > 0:
                    return TradeSignal(
                        side=Side.LONG,
                        stop=stop,
                        target=assumed_entry + stop_dist * self.risk_reward,
                        reason=(
                            f"Long momentum burst: body={body:.2f} ({body / atr:.1f}×ATR) "
                            f"close@{close_position:.1%} of range during {bar.session}."
                        ),
                        tags=("momentum", "burst", bar.session),
                    )

        # ── Short: large bearish body, close in bottom fraction, MACD- ─
        close_bottom = (bar.high - bar.close) / bar_range
        if bar.close < bar.open and close_bottom >= (1.0 - self.body_fraction):
            if macd_hist <= 0:
                assumed_entry = bar.close - slippage
                stop = bar.high
                stop_dist = stop - assumed_entry
                if stop_dist > 0:
                    return TradeSignal(
                        side=Side.SHORT,
                        stop=stop,
                        target=assumed_entry - stop_dist * self.risk_reward,
                        reason=(
                            f"Short momentum burst: body={body:.2f} ({body / atr:.1f}×ATR) "
                            f"close@{close_bottom:.1%} of range during {bar.session}."
                        ),
                        tags=("momentum", "burst", bar.session),
                    )

        return None

    # ------------------------------------------------------------------
    def _macd_histogram(self, bars: Sequence[MarketBar], index: int) -> float:
        """Return current MACD histogram value (positive = bullish momentum)."""
        lookback = (self._macd_slow + self._macd_signal) * 2
        start = max(0, index - lookback + 1)
        prices = [bars[i].close for i in range(start, index + 1)]
        if len(prices) < self._macd_slow:
            return 0.0

        ema_fast = self._ema_series(prices, self._macd_fast)
        ema_slow = self._ema_series(prices, self._macd_slow)
        macd_line = ema_fast - ema_slow

        # Build a short history of MACD line for signal EMA
        # We only need recent bars for signal line
        signal_window = min(len(prices), self._macd_signal * 3)
        ema_fast_hist = [
            self._ema_value(prices[max(0, i - lookback) : i + 1], self._macd_fast)
            for i in range(len(prices) - signal_window, len(prices))
        ]
        ema_slow_hist = [
            self._ema_value(prices[max(0, i - lookback) : i + 1], self._macd_slow)
            for i in range(len(prices) - signal_window, len(prices))
        ]
        macd_hist_vals = [ef - es for ef, es in zip(ema_fast_hist, ema_slow_hist)]

        if len(macd_hist_vals) < self._macd_signal:
            return macd_line

        signal_line = self._ema_series_from_list(macd_hist_vals, self._macd_signal)
        return macd_line - signal_line

    # ------------------------------------------------------------------
    def _ema_series(self, prices: list[float], period: int) -> float:
        return self._ema_value(prices, period)

    def _ema_value(self, prices: list[float], period: int) -> float:
        if not prices:
            return 0.0
        mult = 2.0 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = p * mult + ema * (1.0 - mult)
        return ema

    def _ema_series_from_list(self, values: list[float], period: int) -> float:
        return self._ema_value(values, period)

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
