from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap

_MAX_FVG_SPAN = timedelta(hours=4)


@dataclass(frozen=True)
class FairValueGapStrategy:
    """Fair Value Gap (FVG) retest strategy.

    Logic
    -----
    A Fair Value Gap is a 3-bar imbalance pattern:
      - Bullish FVG at bar k: bars[k-2].high < bars[k].low
        The gap zone spans (bars[k-2].high, bars[k].low).
      - Bearish FVG at bar k: bars[k-2].low > bars[k].high
        The gap zone spans (bars[k].high, bars[k-2].low).

    On each signal bar the strategy scans back *fvg_lookback* bars for the
    most recent unbroken FVG large enough (>= *min_gap_atr* × ATR14).
    An FVG is considered "broken" (invalidated) if any subsequent bar has
    closed inside or beyond the gap midpoint.

    When the current bar taps into an unbroken FVG and the close remains
    within the gap or on the originating side:
      - Bullish retest: bar.low <= fvg_top and bar.close >= fvg_bot
        → LONG, stop = fvg_bot − buffer, target = entry + RR × stop_dist
      - Bearish retest: bar.high >= fvg_bot and bar.close <= fvg_top
        → SHORT, stop = fvg_top + buffer, target = entry − RR × stop_dist

    Gap awareness: ATR lookback gap-checked via the shared helper.
    """

    atr_period: int = 14
    risk_reward: float = 2.0
    max_spread: float = 1.00
    min_gap_atr: float = 0.05       # minimum FVG size as fraction of ATR
    fvg_lookback: int = 20          # bars to scan back for unbroken FVGs
    stop_buffer_atr: float = 0.1    # extra buffer beyond FVG edge for stop
    entry_slippage_buffer: float = 0.1
    name: str = "fair_value_gap"

    def warmup_bars(self) -> int:
        return self.atr_period + self.fvg_lookback + 3

    # ------------------------------------------------------------------
    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        bar = bars[index]


        if bar.spread > self.max_spread:
            return None

        if lookback_spans_gap(bars, index, self.atr_period):
            return None
        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None

        min_gap = self.min_gap_atr * atr
        slippage = self.entry_slippage_buffer * atr
        stop_buf = self.stop_buffer_atr * atr

        # Scan backwards for most recent unbroken FVG
        scan_start = max(2, index - self.fvg_lookback)
        for k in range(index - 1, scan_start - 1, -1):
            # need bars[k-2] to exist
            if k - 2 < 0:
                break

            b_prev2 = bars[k - 2]
            b_curr = bars[k]

            # ── Quality gates: only count FVGs formed in active sessions ──
            # Impulse bar (bars[k-1]) must be during London or NY session
            b_impulse_utc = bars[k - 1].timestamp.astimezone(timezone.utc)
            if not (7 <= b_impulse_utc.hour < 21):
                continue
            # Skip if the 3-bar pattern spans a gap (weekend, daily close)
            if b_curr.timestamp - b_prev2.timestamp > _MAX_FVG_SPAN:
                continue

            # ── Bullish FVG: gap between bars[k-2].high and bars[k].low ──
            if b_prev2.high < b_curr.low:
                fvg_bot = b_prev2.high
                fvg_top = b_curr.low
                if fvg_top - fvg_bot < min_gap:
                    continue

                # Check if FVG has been broken (midpoint violated) by any bar after k
                fvg_mid = (fvg_bot + fvg_top) / 2.0
                broken = False
                for j in range(k + 1, index):
                    if bars[j].close < fvg_mid:
                        broken = True
                        break
                if broken:
                    continue

                # Current bar retests the FVG zone
                if bar.low <= fvg_top and bar.close >= fvg_bot:
                    assumed_entry = bar.close + slippage
                    stop = fvg_bot - stop_buf
                    stop_dist = assumed_entry - stop
                    if stop_dist > 0:
                        return TradeSignal(
                            side=Side.LONG,
                            stop=stop,
                            target=assumed_entry + stop_dist * self.risk_reward,
                            reason=(
                                f"Long FVG retest: gap ({fvg_bot:.2f}–{fvg_top:.2f}), "
                                f"atr={atr:.2f}."
                            ),
                            tags=("fvg", "retest", "long"),
                        )

            # ── Bearish FVG: gap between bars[k].high and bars[k-2].low ──
            elif b_prev2.low > b_curr.high:
                fvg_bot = b_curr.high
                fvg_top = b_prev2.low
                if fvg_top - fvg_bot < min_gap:
                    continue

                # Check if FVG has been broken (midpoint violated upward) by any bar after k
                fvg_mid = (fvg_bot + fvg_top) / 2.0
                broken = False
                for j in range(k + 1, index):
                    if bars[j].close > fvg_mid:
                        broken = True
                        break
                if broken:
                    continue

                # Current bar retests the FVG zone
                if bar.high >= fvg_bot and bar.close <= fvg_top:
                    assumed_entry = bar.close - slippage
                    stop = fvg_top + stop_buf
                    stop_dist = stop - assumed_entry
                    if stop_dist > 0:
                        return TradeSignal(
                            side=Side.SHORT,
                            stop=stop,
                            target=assumed_entry - stop_dist * self.risk_reward,
                            reason=(
                                f"Short FVG retest: gap ({fvg_bot:.2f}–{fvg_top:.2f}), "
                                f"atr={atr:.2f}."
                            ),
                            tags=("fvg", "retest", "short"),
                        )

        return None

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
