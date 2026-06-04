from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class NYCloseCompressionStrategy:
    """NY-close compression range breakout at London open.

    Logic
    -----
    The NY close window (21:00–22:59 UTC) often forms a tight compression
    range as liquidity thins going into the settlement period.  When the
    London session opens (07:00–09:59 UTC) the next day, a break of that
    compressed range frequently kicks off the day's directional move.

    For each bar in the London entry window:
      1. Collect all bars from the *prior* calendar UTC date between
         21:00–22:59 to form the NY-close compression range.
      2. Require the range to meet minimum width: *min_range_atr* × ATR.
      3. If bar.close > range_high + *min_breakout_atr* × ATR → LONG
         If bar.close < range_low  − *min_breakout_atr* × ATR → SHORT
      4. Stop: range midpoint; Target: entry ± RR × stop_dist.

    Gap awareness: ATR lookback gap-checked via the shared helper.
    """

    atr_period: int = 14
    risk_reward: float = 2.0
    max_spread: float = 1.00
    min_breakout_atr: float = 0.05   # minimum extension beyond range edge
    min_range_atr: float = 0.15      # minimum compressed range width
    max_range_atr: float = 0.50      # maximum range width — enforces true compression
    min_range_bars: int = 2          # need at least this many bars in the window
    entry_slippage_buffer: float = 0.1
    name: str = "ny_close_compression"

    # UTC hour boundaries for NY-close collection window (exclusive end)
    _NYCLOSE_START_H: int = 21
    _NYCLOSE_END_H: int = 23

    # UTC hour boundaries for London entry window (exclusive end)
    _LONDON_ENTRY_START_H: int = 7
    _LONDON_ENTRY_END_H: int = 10

    def warmup_bars(self) -> int:
        return self.atr_period + 96  # need prior day's NY-close bars + buffer

    # ------------------------------------------------------------------
    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        bar = bars[index]

        # ── Only fire during London entry window ──────────────────────
        bar_utc_hour = bar.timestamp.astimezone(timezone.utc).hour
        if not (self._LONDON_ENTRY_START_H <= bar_utc_hour < self._LONDON_ENTRY_END_H):
            return None
        if bar.spread > self.max_spread:
            return None

        # ── ATR ───────────────────────────────────────────────────────
        if lookback_spans_gap(bars, index, self.atr_period):
            return None
        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None

        # ── Build prior-day NY-close range ────────────────────────────
        range_high, range_low, count = self._nyclose_range(bars, index)
        if count < self.min_range_bars:
            return None

        range_span = range_high - range_low
        if range_span < self.min_range_atr * atr:
            return None   # range too thin — not a genuine compression
        if range_span > self.max_range_atr * atr:
            return None   # range too wide — not compressed, normal volatility

        range_mid = (range_high + range_low) / 2.0
        min_ext = self.min_breakout_atr * atr
        slippage = self.entry_slippage_buffer * atr

        # ── Long: London bar closes above NY-close range ──────────────
        if bar.close > range_high + min_ext:
            assumed_entry = bar.close + slippage
            stop = range_mid
            stop_dist = assumed_entry - stop
            if stop_dist > 0:
                return TradeSignal(
                    side=Side.LONG,
                    stop=stop,
                    target=assumed_entry + stop_dist * self.risk_reward,
                    reason=(
                        f"Long NY-close compression breakout above {range_high:.2f} "
                        f"(range={range_span:.2f}, atr={atr:.2f})."
                    ),
                    tags=("ny_close_compression", "breakout", "london"),
                )

        # ── Short: London bar closes below NY-close range ─────────────
        if bar.close < range_low - min_ext:
            assumed_entry = bar.close - slippage
            stop = range_mid
            stop_dist = stop - assumed_entry
            if stop_dist > 0:
                return TradeSignal(
                    side=Side.SHORT,
                    stop=stop,
                    target=assumed_entry - stop_dist * self.risk_reward,
                    reason=(
                        f"Short NY-close compression breakdown below {range_low:.2f} "
                        f"(range={range_span:.2f}, atr={atr:.2f})."
                    ),
                    tags=("ny_close_compression", "breakout", "london"),
                )

        return None

    # ------------------------------------------------------------------
    def _nyclose_range(
        self, bars: Sequence[MarketBar], index: int
    ) -> tuple[float, float, int]:
        """Return (high, low, count) for NY-close bars on the prior UTC date."""
        bar_date = bars[index].timestamp.astimezone(timezone.utc).date()

        r_high = -1e18
        r_low = 1e18
        count = 0
        found_prior_day = False

        for i in range(index - 1, -1, -1):
            b = bars[i]
            b_utc = b.timestamp.astimezone(timezone.utc)
            b_date = b_utc.date()

            if b_date >= bar_date:
                continue  # still on current day — skip

            # We're on a prior day
            if not found_prior_day:
                found_prior_day = True  # anchor to this date
                prior_date = b_date

            if b_date < prior_date:  # type: ignore[possibly-undefined]
                break  # gone past the prior day

            if self._NYCLOSE_START_H <= b_utc.hour < self._NYCLOSE_END_H:
                r_high = max(r_high, b.high)
                r_low = min(r_low, b.low)
                count += 1

        if count == 0:
            return 0.0, 0.0, 0
        return r_high, r_low, count

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
