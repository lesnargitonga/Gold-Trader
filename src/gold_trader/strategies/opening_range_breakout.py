from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timezone
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class OpeningRangeBreakoutStrategy:
    """New York session opening-range breakout (NY ORB).

    Logic
    -----
    The first *opening_range_bars* bars of the New York session (13:00 UTC+)
    define the Opening Range.  Once the formation period is complete, a close
    outside the OR — confirmed by minimum ATR extension — signals a
    continuation trade.

    Gold frequently makes its dominant daily directional move at or shortly
    after the NY open, driven by US data releases and equity-market correlation.
    This strategy captures that impulse.

    Distinguished from LondonBreakoutStrategy:
    - Anchored to NY open (13:00 UTC) not London open (07:00 UTC)
    - Fires during the highest-volume gold window (13:30–16:00 UTC)
    - Complementary: if London ORB fails, NY ORB often succeeds

    Stop/Target:
    - Long  stop = or_low,  target = entry + (entry − stop) × RR
    - Short stop = or_high, target = entry − (stop − entry) × RR

    Gap awareness: ATR lookback gap-check.
    """

    opening_range_bars: int = 2      # 2 × 15m = first 30 min of NY session
    atr_period: int = 14
    min_breakout_atr: float = 0.10   # close must exceed OR by this × ATR
    risk_reward: float = 2.0
    max_spread: float = 1.0
    min_news_distance_minutes: float = 30.0
    entry_slippage_buffer: float = 0.1
    name: str = "opening_range_breakout"

    _NY_START_H: int = 13   # 13:00 UTC
    _NY_END_H: int = 21     # 20:59 UTC

    def warmup_bars(self) -> int:
        return self.atr_period + self.opening_range_bars + 4

    # ------------------------------------------------------------------
    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        bar = bars[index]

        # ── basic filters ──────────────────────────────────────────────
        bar_utc = bar.timestamp.astimezone(timezone.utc)
        if not (self._NY_START_H <= bar_utc.hour < self._NY_END_H):
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

        # ── Build today's NY opening range ─────────────────────────────
        or_high, or_low, or_count = self._ny_orb(bars, index)
        if or_count < self.opening_range_bars:
            return None   # ORB formation period not yet complete

        # ── Breakout filter ─────────────────────────────────────────────
        min_extension = self.min_breakout_atr * atr
        slippage = atr * self.entry_slippage_buffer

        # ── Long breakout ──────────────────────────────────────────────
        if bar.close > or_high + min_extension:
            assumed_entry = bar.close + slippage
            stop = or_low
            stop_dist = assumed_entry - stop
            if stop_dist > 0:
                return TradeSignal(
                    side=Side.LONG,
                    stop=stop,
                    target=assumed_entry + stop_dist * self.risk_reward,
                    reason=(
                        f"Long NY ORB breakout above {or_high:.2f} "
                        f"(range={or_high - or_low:.2f}, atr={atr:.2f})."
                    ),
                    tags=("ny_orb", "breakout", "new_york"),
                )

        # ── Short breakout ─────────────────────────────────────────────
        if bar.close < or_low - min_extension:
            assumed_entry = bar.close - slippage
            stop = or_high
            stop_dist = stop - assumed_entry
            if stop_dist > 0:
                return TradeSignal(
                    side=Side.SHORT,
                    stop=stop,
                    target=assumed_entry - stop_dist * self.risk_reward,
                    reason=(
                        f"Short NY ORB breakdown below {or_low:.2f} "
                        f"(range={or_high - or_low:.2f}, atr={atr:.2f})."
                    ),
                    tags=("ny_orb", "breakdown", "new_york"),
                )

        return None

    # ------------------------------------------------------------------
    def _ny_orb(
        self, bars: Sequence[MarketBar], index: int
    ) -> tuple[float, float, int]:
        """Return (high, low, count) for the first N NY-session bars today."""
        bar_date: date = bars[index].timestamp.astimezone(timezone.utc).date()

        ny_bars_today: list[MarketBar] = []
        for i in range(index - 1, -1, -1):
            b = bars[i]
            b_utc = b.timestamp.astimezone(timezone.utc)
            if b_utc.date() < bar_date:
                break
            if self._NY_START_H <= b_utc.hour < self._NY_END_H:
                ny_bars_today.append(b)

        if not ny_bars_today:
            return 0.0, 0.0, 0

        ny_bars_today.reverse()  # chronological order
        orb_bars = ny_bars_today[: self.opening_range_bars]

        if len(orb_bars) < self.opening_range_bars:
            return 0.0, 0.0, 0

        return max(b.high for b in orb_bars), min(b.low for b in orb_bars), len(orb_bars)

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
