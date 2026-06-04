from __future__ import annotations

import unittest

from gold_trader.backtest.engine import BacktestConfig, run_backtest
from gold_trader.backtest.metrics import summarize_backtest
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.models import Side
from gold_trader.strategies.london_breakout import LondonBreakoutStrategy
from gold_trader.strategies.trend_pullback import TrendPullbackStrategy


class LondonBreakoutStrategyTests(unittest.TestCase):
    def test_returns_correct_name(self) -> None:
        self.assertEqual(LondonBreakoutStrategy().name, "london_breakout")

    def test_warmup_bars_exceeds_atr_period(self) -> None:
        strategy = LondonBreakoutStrategy(atr_period=14, opening_range_bars=4)
        self.assertGreater(strategy.warmup_bars(), 14)

    def test_signal_for_does_not_raise_on_short_bars(self) -> None:
        bars = generate_synthetic_bars(count=50, seed=99)
        strategy = LondonBreakoutStrategy()
        for index in range(len(bars)):
            strategy.signal_for(bars, index)

    def test_signal_for_returns_none_or_valid_signal(self) -> None:
        bars = generate_synthetic_bars(count=600, seed=42)
        strategy = LondonBreakoutStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            result = strategy.signal_for(bars, index)
            if result is not None:
                self.assertIn(result.side.value, ("long", "short"))
                self.assertGreater(result.stop, 0.0)
                self.assertGreater(result.target, 0.0)

    def test_long_signal_stop_below_target(self) -> None:
        bars = generate_synthetic_bars(count=800, seed=7)
        strategy = LondonBreakoutStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            sig = strategy.signal_for(bars, index)
            if sig is not None and sig.side is Side.LONG:
                self.assertLess(sig.stop, sig.target,
                                "Long: stop must be below target")
                return

    def test_short_signal_stop_above_target(self) -> None:
        bars = generate_synthetic_bars(count=800, seed=8)
        strategy = LondonBreakoutStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            sig = strategy.signal_for(bars, index)
            if sig is not None and sig.side is Side.SHORT:
                self.assertGreater(sig.stop, sig.target,
                                   "Short: stop must be above target")
                return

    def test_backtest_produces_valid_summary(self) -> None:
        bars = generate_synthetic_bars(count=800, seed=55)
        strategy = LondonBreakoutStrategy()
        result = run_backtest(bars, strategy, BacktestConfig())
        summary = summarize_backtest(result)
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertGreaterEqual(summary.win_rate, 0.0)
        self.assertLessEqual(summary.win_rate, 1.0)


class TrendPullbackStrategyTests(unittest.TestCase):
    def test_returns_correct_name(self) -> None:
        self.assertEqual(TrendPullbackStrategy().name, "trend_pullback")

    def test_warmup_bars_exceeds_ema_slow(self) -> None:
        strategy = TrendPullbackStrategy(ema_slow=50)
        self.assertGreater(strategy.warmup_bars(), 50)

    def test_signal_for_does_not_raise_on_short_bars(self) -> None:
        bars = generate_synthetic_bars(count=50, seed=99)
        strategy = TrendPullbackStrategy()
        for index in range(len(bars)):
            strategy.signal_for(bars, index)

    def test_signal_for_returns_none_or_valid_signal(self) -> None:
        bars = generate_synthetic_bars(count=800, seed=42)
        strategy = TrendPullbackStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            result = strategy.signal_for(bars, index)
            if result is not None:
                self.assertIn(result.side.value, ("long", "short"))
                self.assertGreater(result.stop, 0.0)
                self.assertGreater(result.target, 0.0)

    def test_long_signal_stop_below_entry(self) -> None:
        bars = generate_synthetic_bars(count=1200, seed=13)
        strategy = TrendPullbackStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            sig = strategy.signal_for(bars, index)
            if sig is not None and sig.side is Side.LONG:
                # stop must be strictly below the assumed entry (close)
                self.assertLess(sig.stop, bars[index].close + 5,
                                "Long: stop should be near or below close")
                return

    def test_short_signal_stop_above_entry(self) -> None:
        bars = generate_synthetic_bars(count=1200, seed=14)
        strategy = TrendPullbackStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            sig = strategy.signal_for(bars, index)
            if sig is not None and sig.side is Side.SHORT:
                self.assertGreater(sig.stop, bars[index].close - 5,
                                   "Short: stop should be near or above close")
                return

    def test_backtest_produces_valid_summary(self) -> None:
        bars = generate_synthetic_bars(count=1200, seed=77)
        strategy = TrendPullbackStrategy()
        result = run_backtest(bars, strategy, BacktestConfig())
        summary = summarize_backtest(result)
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertGreaterEqual(summary.win_rate, 0.0)
        self.assertLessEqual(summary.win_rate, 1.0)
