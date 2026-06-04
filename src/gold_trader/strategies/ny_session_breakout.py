from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class NYSessionBreakoutStrategy:
    """New York session breakout of London range.

    Logic
    -----
    The London session (07:00–12:59 UTC) accumulates price action and
    establishes a clear distribution for the European day.  When New York
    opens (13:00 UTC), institutional order flow often resolves directionally —
    either breaking out of, or reversing back within, the London window.

    This strategy bets on the continuation breakout:
      - Collect ALL bars from today's London session as the reference range.
      - Require a minimum range: *min_range_atr* × ATR (suppress flat days).
      - During the NY session (13:00–20:59 UTC), when a bar's *close* (body
        breakout) exceeds the London high or falls below the London low by
        *min_breakout_atr* × ATR → enter in that direction.
      - Stop:   far side of London range (London low for LONG, London high for SHORT).
      - Target: entry ± RR × stop_dist.

    Mechanistic difference from AsianRangeBreakout:
      - Reference range is London (07:00–12:59 UTC) not Asian (00:00–06:59).
      - Signal fires only in NY session, not first-thing at London open.
      - Captures NY institutional flow taking over the London directional move.

    Gap awareness: ATR lookback gap-checks via the shared helper.
    """

    atr_period: int = 14
    risk_reward: float = 2.0
    max_spread: float = 0.75
    min_breakout_atr: float = 0.10
    min_range_atr: float = 0.40      # London range must be substantial
    min_london_bars: int = 3         # minimum bars to have a valid London range
    min_news_distance_minutes: float = 30.0
    entry_slippage_buffer: float = 0.1
    entry_end_hour: int = 21         # stop taking new entries at or after this UTC hour
    require_asian_alignment: bool = False  # Asian session direction must align with signal
    name: str = "ny_session_breakout"

    _LONDON_START_H: int = 7
    _LONDON_END_H: int = 13
    _NY_START_H: int = 13
    _NY_END_H: int = 21

    def warmup_bars(self) -> int:
        return self.atr_period + 24  # need at least one full London session in history

    # ------------------------------------------------------------------
    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        bar = bars[index]

        # ── Only fire during NY session ────────────────────────────────
        bar_utc_hour = bar.timestamp.astimezone(timezone.utc).hour
        if not (self._NY_START_H <= bar_utc_hour < self._NY_END_H):
            return None
        if bar_utc_hour >= self.entry_end_hour:
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

        # ── Build London range for today ───────────────────────────────
        london_high, london_low, london_count = self._london_range(bars, index)
        if london_count < self.min_london_bars:
            return None

        london_span = london_high - london_low
        if london_span < self.min_range_atr * atr:
            return None   # flat / thin London session

        min_ext = self.min_breakout_atr * atr
        slippage = self.entry_slippage_buffer * atr

        # ── Optional Asian alignment filter ───────────────────────────
        asian_bullish: bool | None = None
        if self.require_asian_alignment:
            first_asian_open = self._first_asian_open(bars, index)
            last_asian_close = self._last_asian_close(bars, index)
            if first_asian_open is None or last_asian_close is None:
                return None
            asian_bullish = last_asian_close > first_asian_open

        # ── Long: close breaks above London high ───────────────────────
        if bar.close > london_high + min_ext:
            if self.require_asian_alignment and asian_bullish is not None and not asian_bullish:
                return None
            assumed_entry = bar.close + slippage
            stop = london_low
            stop_dist = assumed_entry - stop
            if stop_dist > 0:
                return TradeSignal(
                    side=Side.LONG,
                    stop=stop,
                    target=assumed_entry + stop_dist * self.risk_reward,
                    reason=(
                        f"Long NY breakout of London high {london_high:.2f} "
                        f"(London range={london_span:.2f}, atr={atr:.2f})."
                    ),
                    tags=("ny_breakout", "london_range", "new_york"),
                )

        # ── Short: close breaks below London low ───────────────────────
        if bar.close < london_low - min_ext:
            if self.require_asian_alignment and asian_bullish is not None and asian_bullish:
                return None
            assumed_entry = bar.close - slippage
            stop = london_high
            stop_dist = stop - assumed_entry
            if stop_dist > 0:
                return TradeSignal(
                    side=Side.SHORT,
                    stop=stop,
                    target=assumed_entry - stop_dist * self.risk_reward,
                    reason=(
                        f"Short NY breakdown of London low {london_low:.2f} "
                        f"(London range={london_span:.2f}, atr={atr:.2f})."
                    ),
                    tags=("ny_breakout", "london_range", "new_york"),
                )

        return None

    # ------------------------------------------------------------------
    def _london_range(
        self, bars: Sequence[MarketBar], index: int
    ) -> tuple[float, float, int]:
        """Return (high, low, count) for all London bars on bar[index]'s UTC date."""
        bar_date = bars[index].timestamp.astimezone(timezone.utc).date()

        london_high = -1e18
        london_low = 1e18
        count = 0

        for i in range(index - 1, -1, -1):
            b = bars[i]
            b_utc = b.timestamp.astimezone(timezone.utc)
            if b_utc.date() < bar_date:
                break
            if self._LONDON_START_H <= b_utc.hour < self._LONDON_END_H:
                london_high = max(london_high, b.high)
                london_low = min(london_low, b.low)
                count += 1

        if count == 0:
            return 0.0, 0.0, 0
        return london_high, london_low, count

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

    # ------------------------------------------------------------------
    _ASIAN_START_H: int = 0
    _ASIAN_END_H: int = 7

    def _asian_range(
        self, bars: Sequence[MarketBar], index: int
    ) -> tuple[float, float, int]:
        """Return (high, low, count) for Asian-session bars on bar[index]'s UTC date."""
        bar_date = bars[index].timestamp.astimezone(timezone.utc).date()
        a_high = -1e18
        a_low = 1e18
        count = 0
        for i in range(index - 1, -1, -1):
            b = bars[i]
            b_utc = b.timestamp.astimezone(timezone.utc)
            if b_utc.date() < bar_date:
                break
            if self._ASIAN_START_H <= b_utc.hour < self._ASIAN_END_H:
                a_high = max(a_high, b.high)
                a_low = min(a_low, b.low)
                count += 1
        if count == 0:
            return 0.0, 0.0, 0
        return a_high, a_low, count

    def _last_asian_close(
        self, bars: Sequence[MarketBar], index: int
    ) -> float | None:
        """Return the close of the last Asian-session bar on bar[index]'s UTC date."""
        bar_date = bars[index].timestamp.astimezone(timezone.utc).date()
        for i in range(index - 1, -1, -1):
            b = bars[i]
            b_utc = b.timestamp.astimezone(timezone.utc)
            if b_utc.date() < bar_date:
                return None
            if self._ASIAN_START_H <= b_utc.hour < self._ASIAN_END_H:
                return b.close
        return None

    def _first_asian_open(
        self, bars: Sequence[MarketBar], index: int
    ) -> float | None:
        """Return the open of the first Asian-session bar on bar[index]'s UTC date.

        Walking backwards, the last Asian bar we encounter is chronologically
        the earliest one — its open is the clean session entry price.
        """
        bar_date = bars[index].timestamp.astimezone(timezone.utc).date()
        first_open: float | None = None
        for i in range(index - 1, -1, -1):
            b = bars[i]
            b_utc = b.timestamp.astimezone(timezone.utc)
            if b_utc.date() < bar_date:
                break
            if self._ASIAN_START_H <= b_utc.hour < self._ASIAN_END_H:
                first_open = b.open  # keep overwriting — last iteration = earliest bar
        return first_open
