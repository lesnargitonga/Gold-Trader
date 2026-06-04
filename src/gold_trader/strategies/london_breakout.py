from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class LondonBreakoutStrategy:
    """London opening-range breakout.

    Logic
    -----
    The first *opening_range_bars* bars of the London session (07:00 UTC+)
    establish a reference range.  Once that formation period is complete, any
    bar in the remainder of London or the NY session that closes cleanly above
    or below that range generates an entry signal.

    This is mechanistically distinct from AsianRangeBreakout:
    - The ORB is tighter (1 h vs 6+ h of Asian consolidation).
    - The breakout typically fires during the London power-hour (08:00–10:00
      UTC) or on the NY open (13:00–14:00 UTC), not immediately at 07:00.
    - False breakouts are suppressed by requiring the *close*, not just the
      high/low, to exceed the range (body-breakout filter).

    Gap awareness: if the ATR lookback spans a gap ≥ 4 h the signal is
    suppressed via the shared helper.
    """

    opening_range_bars: int = 4          # 4 × 15m = first 60 min of London
    atr_period: int = 14
    min_breakout_atr: float = 0.10       # close must be ≥ this × ATR outside range
    risk_reward: float = 2.0
    max_spread: float = 0.75
    min_news_distance_minutes: float = 30.0
    allowed_sessions: tuple[str, ...] = ("london", "new_york")
    entry_slippage_buffer: float = 0.1
    min_atr_threshold: float = 0.0       # volatility regime filter (0 = disabled)
    name: str = "london_breakout"

    _LONDON_START_H: int = 7
    _LONDON_END_H: int = 13   # 07:00–12:59 UTC

    def warmup_bars(self) -> int:
        return self.atr_period + self.opening_range_bars + 4

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

        # ── ATR ────────────────────────────────────────────────────────
        if lookback_spans_gap(bars, index, self.atr_period):
            return None
        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None
        if self.min_atr_threshold > 0.0 and atr < self.min_atr_threshold:
            return None

        # ── Build today's London opening range ─────────────────────────
        orb_high, orb_low, orb_count = self._london_orb(bars, index)
        if orb_count < self.opening_range_bars:
            return None   # formation period not complete

        # ── Breakout filter: close must be outside range ───────────────
        min_extension = self.min_breakout_atr * atr
        slippage = atr * self.entry_slippage_buffer

        # ── Long breakout ──────────────────────────────────────────────
        if bar.close > orb_high + min_extension:
            assumed_entry = bar.close + slippage
            stop = orb_low
            stop_dist = assumed_entry - stop
            if stop_dist > 0:
                return TradeSignal(
                    side=Side.LONG,
                    stop=stop,
                    target=assumed_entry + stop_dist * self.risk_reward,
                    reason=(
                        f"Long London ORB breakout above {orb_high:.2f} "
                        f"(range={orb_high - orb_low:.2f}, atr={atr:.2f}) "
                        f"during {bar.session}."
                    ),
                    tags=("london_orb", "breakout", bar.session),
                )

        # ── Short breakout ─────────────────────────────────────────────
        if bar.close < orb_low - min_extension:
            assumed_entry = bar.close - slippage
            stop = orb_high
            stop_dist = stop - assumed_entry
            if stop_dist > 0:
                return TradeSignal(
                    side=Side.SHORT,
                    stop=stop,
                    target=assumed_entry - stop_dist * self.risk_reward,
                    reason=(
                        f"Short London ORB breakdown below {orb_low:.2f} "
                        f"(range={orb_high - orb_low:.2f}, atr={atr:.2f}) "
                        f"during {bar.session}."
                    ),
                    tags=("london_orb", "breakout", bar.session),
                )

        return None

    # ------------------------------------------------------------------
    def _london_orb(
        self, bars: Sequence[MarketBar], index: int
    ) -> tuple[float, float, int]:
        """Return (high, low, count) for the first N London bars today (UTC)."""
        bar_date = bars[index].timestamp.astimezone(timezone.utc).date()

        london_bars_today: list[MarketBar] = []
        for i in range(index - 1, -1, -1):
            b = bars[i]
            b_utc = b.timestamp.astimezone(timezone.utc)
            if b_utc.date() < bar_date:
                break
            if self._LONDON_START_H <= b_utc.hour < self._LONDON_END_H:
                london_bars_today.append(b)

        if not london_bars_today:
            return 0.0, 0.0, 0

        # Reverse to chronological order so we can take the *first* N bars
        london_bars_today.reverse()
        orb_bars = london_bars_today[: self.opening_range_bars]

        if len(orb_bars) < self.opening_range_bars:
            return 0.0, 0.0, 0

        orb_high = max(b.high for b in orb_bars)
        orb_low = min(b.low for b in orb_bars)
        return orb_high, orb_low, len(orb_bars)

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
