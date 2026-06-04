from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta, timezone
from typing import ClassVar, Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap

# ── session hour boundaries (UTC) ─────────────────────────────────────────
_LONDON_START_H: int = 7
_LONDON_END_H: int = 13
_NY_START_H: int = 13
_NY_EARLY_END_H: int = 15
_ASIAN_START_H: int = 0
_ASIAN_END_H: int = 7


@dataclass(frozen=True)
class SessionContinuationStrategy:
    """Consecutive Session Continuation — regime-filtered mean-reversion.

    Logic (Concept D from evaluation.md)
    --------------------------------------
    If the London session closes in the top/bottom quantile of that day's
    trading range AND the first two hours of the NY session extend in that
    same direction, the Asian pullback back into the combined London+NY range
    provides a high-probability mean-reversion entry with the direction of
    institutional flow.

    Conditions:
    1. London closed in the top *quantile* of its session range (bullish) or
       bottom of its range (bearish).  Measured as:
         (london_close − london_low) / london_range ≥ min_session_quantile  → bullish
         (london_high − london_close) / london_range ≥ min_session_quantile → bearish
    2. NY early bars (13:00–14:59 UTC) confirm: for LONG, NY early high > London high;
       for SHORT, NY early low < London low.
    3. Current bar is in the Asian session (00:00–06:59 UTC) of the NEXT day.
    4. Price has pulled back into the combined London+NY range.
    5. Asian ATR check: current bar's close is within the prior combined range.

    Entry: bar.close with slippage
    Stop:  combined range midpoint
    Target: re-test of London-session close level (or RR-parametric)

    Gap awareness: ATR lookback gap-checked via the shared helper.
    """

    atr_period: int = 14
    risk_reward: float = 1.5
    max_spread: float = 1.00
    min_session_quantile: float = 0.6
    min_range_atr: float = 0.30
    entry_slippage_buffer: float = 0.1
    name: ClassVar[str] = "session_continuation"

    _LONDON_START_H: ClassVar[int] = _LONDON_START_H
    _LONDON_END_H: ClassVar[int] = _LONDON_END_H
    _NY_START_H: ClassVar[int] = _NY_START_H
    _NY_EARLY_END_H: ClassVar[int] = _NY_EARLY_END_H
    _ASIAN_START_H: ClassVar[int] = _ASIAN_START_H
    _ASIAN_END_H: ClassVar[int] = _ASIAN_END_H

    def warmup_bars(self) -> int:
        return self.atr_period + 100  # need prior full London + NY session

    # ------------------------------------------------------------------
    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        bar = bars[index]

        # ── Only fire during Asian session ────────────────────────────
        bar_utc = bar.timestamp.astimezone(timezone.utc)
        if not (_ASIAN_START_H <= bar_utc.hour < _ASIAN_END_H):
            return None
        if bar.spread > self.max_spread:
            return None

        if lookback_spans_gap(bars, index, self.atr_period):
            return None
        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None

        # ── Collect prior day's London session bars ───────────────────
        bar_date = bar_utc.date()
        prior_date = bar_date - timedelta(days=1)
        # On weekdays prior_date may still be a trading day;
        # we scan backwards to find the most recent London session.

        lon_high, lon_low, lon_close, lon_count = self._london_session(bars, index)
        if lon_count < 3:
            return None

        london_range = lon_high - lon_low
        if london_range < self.min_range_atr * atr:
            return None

        # ── London quantile check ─────────────────────────────────────
        lon_quantile = (lon_close - lon_low) / london_range if london_range > 0 else 0.5
        bullish = lon_quantile >= self.min_session_quantile
        bearish = lon_quantile <= (1.0 - self.min_session_quantile)
        if not bullish and not bearish:
            return None

        # ── NY early confirmation ─────────────────────────────────────
        ny_high, ny_low, ny_count = self._ny_early(bars, index)
        if ny_count < 1:
            return None

        combined_high = max(lon_high, ny_high)
        combined_low = min(lon_low, ny_low)
        combined_range = combined_high - combined_low
        if combined_range < self.min_range_atr * atr:
            return None

        combined_mid = (combined_high + combined_low) / 2.0
        slippage = self.entry_slippage_buffer * atr

        if bullish:
            # NY must have extended above London high for confirmation
            if ny_high <= lon_high:
                return None
            # Asian bar must be pulling back into the combined range
            if bar.close >= lon_high:
                return None   # hasn't pulled back yet
            if bar.close < combined_low:
                return None   # gap down past the range — signal invalid
            # Entry: LONG on pullback into range
            assumed_entry = bar.close + slippage
            stop = combined_low - slippage
            stop_dist = assumed_entry - stop
            if stop_dist <= 0:
                return None
            return TradeSignal(
                side=Side.LONG,
                stop=stop,
                target=assumed_entry + stop_dist * self.risk_reward,
                reason=(
                    f"Long session continuation: LDN q={lon_quantile:.0%} "
                    f"NY extended to {ny_high:.2f}, Asian pullback to {bar.close:.2f}."
                ),
                tags=("session_continuation", "mean_reversion", "asian"),
            )

        if bearish:
            # NY must have extended below London low for confirmation
            if ny_low >= lon_low:
                return None
            # Asian bar must be pulling back into the combined range
            if bar.close <= lon_low:
                return None   # hasn't pulled back yet
            if bar.close > combined_high:
                return None   # gap up past the range — signal invalid
            # Entry: SHORT on pullback into range
            assumed_entry = bar.close - slippage
            stop = combined_high + slippage
            stop_dist = stop - assumed_entry
            if stop_dist <= 0:
                return None
            return TradeSignal(
                side=Side.SHORT,
                stop=stop,
                target=assumed_entry - stop_dist * self.risk_reward,
                reason=(
                    f"Short session continuation: LDN q={lon_quantile:.0%} "
                    f"NY extended to {ny_low:.2f}, Asian pullback to {bar.close:.2f}."
                ),
                tags=("session_continuation", "mean_reversion", "asian"),
            )

        return None

    # ------------------------------------------------------------------
    def _london_session(
        self, bars: Sequence[MarketBar], index: int
    ) -> tuple[float, float, float, int]:
        """Return (high, low, close_of_last_london_bar, count) for the most recent London session."""
        # Walk backwards to find the last London session (not same UTC date as bar[index])
        bar_date = bars[index].timestamp.astimezone(timezone.utc).date()

        lon_high = -1e18
        lon_low = 1e18
        lon_close = 0.0
        count = 0
        found_london_date: date | None = None

        for i in range(index - 1, -1, -1):
            b = bars[i]
            b_utc = b.timestamp.astimezone(timezone.utc)
            b_date = b_utc.date()

            if b_date == bar_date:
                continue   # same day as Asian retest bar — skip

            if found_london_date is None:
                if _LONDON_START_H <= b_utc.hour < _LONDON_END_H:
                    found_london_date = b_date
                    lon_close = b.close  # first London bar found (most recent)
                else:
                    continue

            if b_date < found_london_date:
                break

            if _LONDON_START_H <= b_utc.hour < _LONDON_END_H:
                lon_high = max(lon_high, b.high)
                lon_low = min(lon_low, b.low)
                count += 1

        if count == 0:
            return 0.0, 0.0, 0.0, 0
        return lon_high, lon_low, lon_close, count

    def _ny_early(
        self, bars: Sequence[MarketBar], index: int
    ) -> tuple[float, float, int]:
        """Return (high, low, count) for NY early bars (13:00–14:59) on the same day as London."""
        bar_date = bars[index].timestamp.astimezone(timezone.utc).date()

        ny_high = -1e18
        ny_low = 1e18
        count = 0
        found_ny_date: date | None = None

        for i in range(index - 1, -1, -1):
            b = bars[i]
            b_utc = b.timestamp.astimezone(timezone.utc)
            b_date = b_utc.date()

            if b_date == bar_date:
                continue

            if found_ny_date is None:
                if _NY_START_H <= b_utc.hour < _NY_EARLY_END_H:
                    found_ny_date = b_date
                else:
                    continue

            if b_date < found_ny_date:
                break

            if _NY_START_H <= b_utc.hour < _NY_EARLY_END_H:
                ny_high = max(ny_high, b.high)
                ny_low = min(ny_low, b.low)
                count += 1

        if count == 0:
            return 0.0, 0.0, 0
        return ny_high, ny_low, count

    def _atr(self, bars: Sequence[MarketBar], index: int) -> float:
        start = index - self.atr_period + 1
        atr_bars = bars[start : index + 1]
        previous_close = bars[start - 1].close if start > 0 else None
        true_ranges: list[float] = []
        for b in atr_bars:
            true_ranges.append(b.true_range(previous_close))
            previous_close = b.close
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0
