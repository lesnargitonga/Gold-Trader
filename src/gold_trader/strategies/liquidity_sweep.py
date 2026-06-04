from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class LiquiditySweepStrategy:
    lookback: int = 20
    atr_period: int = 14
    min_sweep_atr: float = 0.2
    risk_reward: float = 2.0
    max_spread: float = 0.75
    min_news_distance_minutes: float = 30.0
    allowed_sessions: tuple[str, ...] = ("london", "new_york")
    entry_slippage_buffer: float = 0.1
    name: str = "liquidity_sweep_reversal"

    def warmup_bars(self) -> int:
        return max(self.lookback, self.atr_period) + 1

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

        history = bars[index - self.lookback:index]
        if len(history) < self.lookback:
            return None

        if lookback_spans_gap(bars, index, self.lookback):
            return None

        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None

        prior_high = max(history_bar.high for history_bar in history)
        prior_low = min(history_bar.low for history_bar in history)
        slippage = atr * self.entry_slippage_buffer
        candidates: list[tuple[float, TradeSignal]] = []

        if bar.low < prior_low and bar.close > prior_low:
            sweep_size = prior_low - bar.low
            if sweep_size >= self.min_sweep_atr * atr:
                assumed_entry = bar.close + slippage
                stop_dist = assumed_entry - bar.low
                if stop_dist <= 0.0:
                    pass
                else:
                    candidates.append(
                        (
                            sweep_size / atr,
                            TradeSignal(
                                side=Side.LONG,
                                stop=bar.low,
                                target=assumed_entry + stop_dist * self.risk_reward,
                                reason=(
                                    "Long liquidity sweep reversal after taking prior lows "
                                    f"during {bar.session}."
                                ),
                                tags=("liquidity", "reversal", bar.session),
                            ),
                        )
                    )

        if bar.high > prior_high and bar.close < prior_high:
            sweep_size = bar.high - prior_high
            if sweep_size >= self.min_sweep_atr * atr:
                assumed_entry = bar.close - slippage
                stop_dist = bar.high - assumed_entry
                if stop_dist <= 0.0:
                    pass
                else:
                    candidates.append(
                        (
                            sweep_size / atr,
                            TradeSignal(
                                side=Side.SHORT,
                                stop=bar.high,
                                target=assumed_entry - stop_dist * self.risk_reward,
                                reason=(
                                    "Short liquidity sweep reversal after taking prior highs "
                                    f"during {bar.session}."
                                ),
                                tags=("liquidity", "reversal", bar.session),
                            ),
                        )
                    )

        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def _atr(self, bars: Sequence[MarketBar], index: int) -> float:
        start = index - self.atr_period + 1
        atr_bars = bars[start:index + 1]
        previous_close = bars[start - 1].close if start > 0 else None
        true_ranges: list[float] = []

        for bar in atr_bars:
            true_ranges.append(bar.true_range(previous_close))
            previous_close = bar.close

        return sum(true_ranges) / len(true_ranges)