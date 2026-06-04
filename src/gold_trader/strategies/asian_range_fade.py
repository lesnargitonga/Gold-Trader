from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timezone
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class AsianRangeFadeStrategy:
    """Asian session range fade (mean-reversion counterpart to AsianRangeBreakout).

    Logic
    -----
    When price tests the Asian session high or low during early London, but
    *fails to close beyond it* — leaving a rejection wick — this signals a
    mean-reversion trade back toward the centre of the Asian range.

    This strategy is the complementary pair to AsianRangeBreakout:
    - AsianRangeBreakout: trades the clean breakout continuation
    - AsianRangeFade:     trades the failed breakout reversal (wick rejection)

    Rejection criteria:
    - LONG fade:  bar.low  ≤ asian_low  AND bar.close > asian_low   (wick below, close inside)
                  wick size (asian_low − bar.low) ≥ min_rejection_atr × ATR
    - SHORT fade: bar.high ≥ asian_high AND bar.close < asian_high  (wick above, close inside)
                  wick size (bar.high − asian_high) ≥ min_rejection_atr × ATR

    Only fires in early London (07:00–10:59 UTC) — after this window the Asian
    range has less significance as price discovery widens.

    Stop:
    - Long  stop = asian_low  − stop_atr_buffer × ATR  (below the rejected extreme)
    - Short stop = asian_high + stop_atr_buffer × ATR

    Target: entry ± (entry − stop) × risk_reward

    Gap awareness: ATR lookback gap-check.
    """

    atr_period: int = 14
    risk_reward: float = 1.5        # lower RR — fade trades revert to midpoint
    max_spread: float = 1.0
    min_rejection_atr: float = 0.15  # minimum wick to count as rejection
    min_range_atr: float = 0.20      # Asian range must be meaningful (not flat)
    min_asian_bars: int = 3
    stop_atr_buffer: float = 0.3    # stop placed this × ATR beyond Asian extreme
    min_news_distance_minutes: float = 30.0
    entry_slippage_buffer: float = 0.1
    name: str = "asian_range_fade"

    _ASIAN_START_H: int = 0
    _ASIAN_END_H: int = 7     # 00:00–06:59 UTC
    _FADE_START_H: int = 7
    _FADE_END_H: int = 11     # fade window 07:00–10:59 UTC (early London)

    def warmup_bars(self) -> int:
        return self.atr_period + self.min_asian_bars + 1

    # ------------------------------------------------------------------
    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        bar = bars[index]
        bar_utc = bar.timestamp.astimezone(timezone.utc)

        # ── session: only early London fade window ─────────────────────
        if not (self._FADE_START_H <= bar_utc.hour < self._FADE_END_H):
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

        # ── ATR ────────────────────────────────────────────────────────
        if lookback_spans_gap(bars, index, self.atr_period):
            return None
        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None

        # ── Build Asian range ──────────────────────────────────────────
        asian_high, asian_low, asian_count = self._asian_range(bars, index)
        if asian_count < self.min_asian_bars:
            return None

        asian_range = asian_high - asian_low
        if asian_range < self.min_range_atr * atr:
            return None

        stop_buffer = self.stop_atr_buffer * atr
        slippage = self.entry_slippage_buffer * atr
        min_rejection = self.min_rejection_atr * atr

        # ── Long fade: wick below asian_low, close back inside ─────────
        if bar.low <= asian_low and bar.close > asian_low:
            wick_size = asian_low - bar.low
            if wick_size >= min_rejection:
                assumed_entry = bar.close + slippage
                stop = asian_low - stop_buffer
                stop_dist = assumed_entry - stop
                if stop_dist > 0:
                    return TradeSignal(
                        side=Side.LONG,
                        stop=stop,
                        target=assumed_entry + stop_dist * self.risk_reward,
                        reason=(
                            f"Long Asian fade: wick={wick_size:.2f} below asian_low={asian_low:.2f} "
                            f"rejected, close={bar.close:.2f} back inside range "
                            f"(range={asian_range:.2f}, atr={atr:.2f})."
                        ),
                        tags=("asian_range", "fade", "rejection"),
                    )

        # ── Short fade: wick above asian_high, close back inside ───────
        if bar.high >= asian_high and bar.close < asian_high:
            wick_size = bar.high - asian_high
            if wick_size >= min_rejection:
                assumed_entry = bar.close - slippage
                stop = asian_high + stop_buffer
                stop_dist = stop - assumed_entry
                if stop_dist > 0:
                    return TradeSignal(
                        side=Side.SHORT,
                        stop=stop,
                        target=assumed_entry - stop_dist * self.risk_reward,
                        reason=(
                            f"Short Asian fade: wick={wick_size:.2f} above asian_high={asian_high:.2f} "
                            f"rejected, close={bar.close:.2f} back inside range "
                            f"(range={asian_range:.2f}, atr={atr:.2f})."
                        ),
                        tags=("asian_range", "fade", "rejection"),
                    )

        return None

    # ------------------------------------------------------------------
    def _asian_range(
        self, bars: Sequence[MarketBar], index: int
    ) -> tuple[float, float, int]:
        """Return (high, low, count) for Asian-session bars on bar[index]'s UTC date."""
        bar_date: date = bars[index].timestamp.astimezone(timezone.utc).date()

        asian_bars: list[MarketBar] = []
        for i in range(index - 1, -1, -1):
            b = bars[i]
            b_utc = b.timestamp.astimezone(timezone.utc)
            if b_utc.date() < bar_date:
                break
            if self._ASIAN_START_H <= b_utc.hour < self._ASIAN_END_H:
                asian_bars.append(b)

        if not asian_bars:
            return 0.0, 0.0, 0

        return (
            max(b.high for b in asian_bars),
            min(b.low for b in asian_bars),
            len(asian_bars),
        )

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
