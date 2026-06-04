from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timezone
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from . import filters as F
from .base import lookback_spans_gap


_DEFAULT_ARB_FILTERS = (
    "htf_trend",        # HIGH — 4H trend alignment
    "hour_window",      # CRITICAL — entry within 07:00–10:00 UTC
    "spread_relative",  # MEDIUM — breakout-bar spread <= 1.2x mean
)


@dataclass(frozen=True)
class AsianRangeBreakoutStrategy:
    """Asian session range breakout.

    Logic
    -----
    The Asian session (00:00–06:59 UTC) sets an overnight consolidation range.
    When London opens, the first clean breakout above the Asian high or below
    the Asian low often marks the day's directional move.

    For each bar in the London or New York session:
    - Build the Asian range from bars on the same calendar day (UTC) that fall
      in the Asia session window.
    - Require at least *min_asian_bars* to have a meaningful range.
    - Breakout up   → LONG  stop≈asian_low   target = entry + RR×(entry−stop)
    - Breakout down → SHORT stop≈asian_high  target = entry − RR×(stop−entry)
    - The breakout must clear the Asian extreme by *min_breakout_atr* × ATR to
      suppress noise breakouts.
    - Require a minimum Asian range: *min_range_atr* × ATR (avoids flat holiday
      sessions where any tick is a "breakout").

    Gap awareness: if the lookback used for ATR spans a gap longer than 4 h the
    signal is suppressed via the shared helper.
    """

    atr_period: int = 14
    risk_reward: float = 2.0
    max_spread: float = 1.00
    min_breakout_atr: float = 0.05
    min_range_atr: float = 0.50      # spec: ≥0.5×ATR (raised from 0.30)
    min_asian_bars: int = 8          # spec: ≥8 bars (raised from 4)
    min_news_distance_minutes: float = 60.0  # spec: 60min news clearance
    allowed_sessions: tuple[str, ...] = ("london", "new_york")
    entry_slippage_buffer: float = 0.1
    # Discretionary checklist filters (HANDBOOK §11 Option E).
    filters_enabled: tuple[str, ...] = _DEFAULT_ARB_FILTERS
    entry_hour_start: int = 7
    entry_hour_end: int = 10
    htf_minutes: int = 240
    htf_ema_fast: int = 20
    htf_ema_slow: int = 50
    min_atr_threshold: float = 0.0   # volatility regime filter: skip if ATR < this (0.0 = disabled)
    max_atr_threshold: float = 0.0   # volatility regime filter: skip if ATR > this (0.0 = disabled)
    min_risk_atr: float = 0.0        # reject if stop_dist < min_risk_atr × ATR; 0.0 = disabled
    name: str = "asian_range_breakout"

    # UTC hour boundaries (inclusive start, exclusive end)
    _ASIAN_SESSION_START_H: int = 0
    _ASIAN_SESSION_END_H: int = 7   # 00:00–06:59 UTC

    def warmup_bars(self) -> int:
        return self.atr_period + self.min_asian_bars + 1

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
        if self.max_atr_threshold > 0.0 and atr > self.max_atr_threshold:
            return None

        # ── Build today's Asian range ──────────────────────────────────
        asian_high, asian_low, asian_bar_count = self._asian_range(bars, index)
        if asian_bar_count < self.min_asian_bars:
            return None

        asian_range = asian_high - asian_low
        if asian_range < self.min_range_atr * atr:
            return None  # flat / holiday session

        slippage = atr * self.entry_slippage_buffer
        min_extension = self.min_breakout_atr * atr

        # ── Long breakout ──────────────────────────────────────────────
        if bar.high > asian_high + min_extension:
            ok, why = F.apply_generic_filters(
                bars, index, Side.LONG, self.filters_enabled,
                cfg={
                    "htf_minutes": self.htf_minutes,
                    "htf_ema_fast": self.htf_ema_fast,
                    "htf_ema_slow": self.htf_ema_slow,
                    "hour_start": self.entry_hour_start,
                    "hour_end": self.entry_hour_end,
                },
            )
            if not ok:
                object.__setattr__(self, "_last_filter_rejection", why)
                return None
            assumed_entry = bar.close + slippage
            stop = asian_low
            stop_dist = assumed_entry - stop
            if stop_dist <= 0:
                return None
            return TradeSignal(
                side=Side.LONG,
                stop=stop,
                target=assumed_entry + stop_dist * self.risk_reward,
                reason=(
                    f"Long Asian-range breakout above {asian_high:.2f} "
                    f"(range={asian_range:.2f}, atr={atr:.2f}) during {bar.session}."
                ),
                tags=("asian_range", "breakout", bar.session),
                risk_reward=self.risk_reward,
            )

        # ── Short breakout ─────────────────────────────────────────────
        if bar.low < asian_low - min_extension:
            ok, why = F.apply_generic_filters(
                bars, index, Side.SHORT, self.filters_enabled,
                cfg={
                    "htf_minutes": self.htf_minutes,
                    "htf_ema_fast": self.htf_ema_fast,
                    "htf_ema_slow": self.htf_ema_slow,
                    "hour_start": self.entry_hour_start,
                    "hour_end": self.entry_hour_end,
                },
            )
            if not ok:
                object.__setattr__(self, "_last_filter_rejection", why)
                return None
            assumed_entry = bar.close - slippage
            stop = asian_high
            stop_dist = stop - assumed_entry
            if stop_dist <= 0:
                return None
            return TradeSignal(
                side=Side.SHORT,
                stop=stop,
                target=assumed_entry - stop_dist * self.risk_reward,
                reason=(
                    f"Short Asian-range breakdown below {asian_low:.2f} "
                    f"(range={asian_range:.2f}, atr={atr:.2f}) during {bar.session}."
                ),
                tags=("asian_range", "breakout", bar.session),
                risk_reward=self.risk_reward,
            )

        return None

    # ------------------------------------------------------------------
    def _asian_range(
        self, bars: Sequence[MarketBar], index: int
    ) -> tuple[float, float, int]:
        """Return (high, low, count) for Asian-session bars on bar[index]'s UTC date."""
        bar_date: date = bars[index].timestamp.astimezone(timezone.utc).date()

        asian_high = -1e18
        asian_low = 1e18
        count = 0

        # Walk backwards from index-1 to collect Asian bars from SAME calendar day
        for i in range(index - 1, -1, -1):
            b = bars[i]
            b_utc = b.timestamp.astimezone(timezone.utc)
            b_date = b_utc.date()

            if b_date < bar_date:
                # Yesterday – stop unless it's an Asian bar from yesterday
                # (relevant when breakout bar is early London)
                if b_date == bar_date or b_utc.hour < self._ASIAN_SESSION_END_H:
                    # Include Asian bars from the previous calendar day when
                    # today has none yet (typical: bar[index] is 07:00 bar)
                    if count == 0 and b_utc.hour < self._ASIAN_SESSION_END_H:
                        asian_high = max(asian_high, b.high)
                        asian_low = min(asian_low, b.low)
                        count += 1
                        bar_date = b_date  # anchor to previous day now
                        continue
                break

            if self._ASIAN_SESSION_START_H <= b_utc.hour < self._ASIAN_SESSION_END_H:
                asian_high = max(asian_high, b.high)
                asian_low = min(asian_low, b.low)
                count += 1

        if count == 0:
            return 0.0, 0.0, 0

        return asian_high, asian_low, count

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
