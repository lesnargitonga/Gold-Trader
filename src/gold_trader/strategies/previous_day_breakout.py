from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timezone
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class PreviousDayBreakoutStrategy:
    """Previous calendar-day high/low breakout.

    Logic
    -----
    The prior UTC day's high (PDH) and low (PDL) are major reference levels
    watched by institutional gold traders and algorithmic systems.  A clean
    close beyond these levels — confirmed by minimum momentum — often signals
    continuation to the next significant level.

    Construction:
    - Scan back from the current bar to collect bars from the *previous*
      full calendar day (UTC).  Require at least *min_prev_day_bars* found.
    - PDH = max(prev_day_bars.high), PDL = min(prev_day_bars.low)

    Entry:
    - Long  if bar.close > PDH + min_breakout_atr × ATR
    - Short if bar.close < PDL − min_breakout_atr × ATR

    Stop:
    - Long  stop = PDH − stop_atr_buffer × ATR  (just below breakout level)
    - Short stop = PDL + stop_atr_buffer × ATR

    Target: entry ± (entry − stop) × risk_reward

    Gap awareness: ATR lookback gap-check.
    Session filter: London + New York only.
    """

    atr_period: int = 14
    risk_reward: float = 2.0
    max_spread: float = 1.0
    min_breakout_atr: float = 0.05   # bar.close must clear PDH/PDL by this × ATR
    stop_atr_buffer: float = 0.5     # stop = PDH − buffer×ATR (long) etc.
    min_prev_day_bars: int = 4
    min_news_distance_minutes: float = 30.0
    allowed_sessions: tuple[str, ...] = ("london", "new_york")
    entry_slippage_buffer: float = 0.1
    name: str = "previous_day_breakout"

    def warmup_bars(self) -> int:
        # need at least atr_period + enough bars from prev day
        return self.atr_period + 100  # 100 × 15m = 25h covers one full prev day

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

        # ── ATR ────────────────────────────────────────────────────────
        if lookback_spans_gap(bars, index, self.atr_period):
            return None
        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None

        # ── Previous-day levels ────────────────────────────────────────
        pdh, pdl, prev_count = self._prev_day_levels(bars, index)
        if prev_count < self.min_prev_day_bars:
            return None

        min_extension = self.min_breakout_atr * atr
        stop_buffer = self.stop_atr_buffer * atr
        slippage = self.entry_slippage_buffer * atr

        # ── Long breakout above PDH ───────────────────────────────────
        if bar.close > pdh + min_extension:
            assumed_entry = bar.close + slippage
            stop = pdh - stop_buffer
            stop_dist = assumed_entry - stop
            if stop_dist > 0:
                return TradeSignal(
                    side=Side.LONG,
                    stop=stop,
                    target=assumed_entry + stop_dist * self.risk_reward,
                    reason=(
                        f"Long PDH breakout: close={bar.close:.2f} > PDH={pdh:.2f} "
                        f"(+{bar.close - pdh:.2f}) atr={atr:.2f} during {bar.session}."
                    ),
                    tags=("prev_day", "breakout", bar.session),
                )

        # ── Short breakdown below PDL ─────────────────────────────────
        if bar.close < pdl - min_extension:
            assumed_entry = bar.close - slippage
            stop = pdl + stop_buffer
            stop_dist = stop - assumed_entry
            if stop_dist > 0:
                return TradeSignal(
                    side=Side.SHORT,
                    stop=stop,
                    target=assumed_entry - stop_dist * self.risk_reward,
                    reason=(
                        f"Short PDL breakdown: close={bar.close:.2f} < PDL={pdl:.2f} "
                        f"({bar.close - pdl:.2f}) atr={atr:.2f} during {bar.session}."
                    ),
                    tags=("prev_day", "breakdown", bar.session),
                )

        return None

    # ------------------------------------------------------------------
    def _prev_day_levels(
        self, bars: Sequence[MarketBar], index: int
    ) -> tuple[float, float, int]:
        """Return (PDH, PDL, bar_count) from the previous UTC calendar day."""
        current_date: date = bars[index].timestamp.astimezone(timezone.utc).date()

        prev_day_bars: list[MarketBar] = []
        prev_date: date | None = None

        for i in range(index - 1, -1, -1):
            b = bars[i]
            b_date = b.timestamp.astimezone(timezone.utc).date()

            if b_date == current_date:
                continue  # still today

            if prev_date is None:
                prev_date = b_date  # first prev-day bar found

            if b_date < prev_date:
                break  # gone further than one day back

            prev_day_bars.append(b)

        if not prev_day_bars:
            return 0.0, 0.0, 0

        pdh = max(b.high for b in prev_day_bars)
        pdl = min(b.low for b in prev_day_bars)
        return pdh, pdl, len(prev_day_bars)

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
