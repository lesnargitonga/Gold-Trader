from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class CompressionBreakoutStrategy:
    breakout_lookback: int = 12
    compression_lookback: int = 6
    atr_period: int = 14
    max_compression_atr_ratio: float = 2.0
    min_breakout_atr: float = 0.1
    risk_reward: float = 2.0
    max_spread: float = 0.75
    min_news_distance_minutes: float = 30.0
    allowed_sessions: tuple[str, ...] = ("london", "new_york")
    entry_slippage_buffer: float = 0.1
    min_atr_threshold: float = 0.0   # volatility regime filter (0 = disabled)
    name: str = "compression_breakout_continuation"

    def warmup_bars(self) -> int:
        return max(self.breakout_lookback, self.compression_lookback, self.atr_period) + 1

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

        breakout_history = bars[index - self.breakout_lookback:index]
        compression_history = bars[index - self.compression_lookback:index]
        if len(breakout_history) < self.breakout_lookback:
            return None
        if len(compression_history) < self.compression_lookback:
            return None

        if lookback_spans_gap(bars, index, self.breakout_lookback):
            return None

        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None
        if self.min_atr_threshold > 0.0 and atr < self.min_atr_threshold:
            return None

        compression_high = max(history_bar.high for history_bar in compression_history)
        compression_low = min(history_bar.low for history_bar in compression_history)
        compression_range = compression_high - compression_low
        if compression_range > atr * self.max_compression_atr_ratio:
            return None

        prior_high = max(history_bar.high for history_bar in breakout_history)
        prior_low = min(history_bar.low for history_bar in breakout_history)
        breakout_threshold = atr * self.min_breakout_atr
        slippage = atr * self.entry_slippage_buffer

        if bar.close > prior_high and bar.close - prior_high >= breakout_threshold:
            assumed_entry = bar.close + slippage
            stop = compression_low
            if stop >= assumed_entry:
                return None
            return TradeSignal(
                side=Side.LONG,
                stop=stop,
                target=assumed_entry + (assumed_entry - stop) * self.risk_reward,
                reason=(
                    "Long compression breakout after range contraction and upside expansion "
                    f"during {bar.session}."
                ),
                tags=("compression", "breakout", bar.session),
            )

        if bar.close < prior_low and prior_low - bar.close >= breakout_threshold:
            assumed_entry = bar.close - slippage
            stop = compression_high
            if stop <= assumed_entry:
                return None
            return TradeSignal(
                side=Side.SHORT,
                stop=stop,
                target=assumed_entry - (stop - assumed_entry) * self.risk_reward,
                reason=(
                    "Short compression breakout after range contraction and downside expansion "
                    f"during {bar.session}."
                ),
                tags=("compression", "breakout", bar.session),
            )

        return None

    def _atr(self, bars: Sequence[MarketBar], index: int) -> float:
        start = index - self.atr_period + 1
        atr_bars = bars[start:index + 1]
        previous_close = bars[start - 1].close if start > 0 else None
        true_ranges: list[float] = []

        for bar in atr_bars:
            true_ranges.append(bar.true_range(previous_close))
            previous_close = bar.close

        return sum(true_ranges) / len(true_ranges)