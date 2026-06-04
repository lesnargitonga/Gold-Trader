from __future__ import annotations

import unittest

from gold_trader.backtest.engine import BacktestConfig, run_backtest
from gold_trader.backtest.metrics import summarize_backtest
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.models import Side
from gold_trader.strategies.ny_session_breakout import NYSessionBreakoutStrategy
from gold_trader.strategies.momentum_burst import MomentumBurstStrategy


class NYSessionBreakoutStrategyTests(unittest.TestCase):
    def test_returns_correct_name(self) -> None:
        self.assertEqual(NYSessionBreakoutStrategy().name, "ny_session_breakout")

    def test_warmup_bars_reasonable(self) -> None:
        strategy = NYSessionBreakoutStrategy(atr_period=14)
        self.assertGreater(strategy.warmup_bars(), 14)

    def test_signal_for_does_not_raise_on_short_bars(self) -> None:
        bars = generate_synthetic_bars(count=50, seed=99)
        strategy = NYSessionBreakoutStrategy()
        for index in range(len(bars)):
            strategy.signal_for(bars, index)

    def test_signal_for_returns_none_or_valid_signal(self) -> None:
        bars = generate_synthetic_bars(count=600, seed=42)
        strategy = NYSessionBreakoutStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            result = strategy.signal_for(bars, index)
            if result is not None:
                self.assertIn(result.side.value, ("long", "short"))
                self.assertGreater(result.stop, 0.0)
                self.assertGreater(result.target, 0.0)

    def test_long_signal_stop_below_target(self) -> None:
        bars = generate_synthetic_bars(count=800, seed=7)
        strategy = NYSessionBreakoutStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            sig = strategy.signal_for(bars, index)
            if sig is not None and sig.side is Side.LONG:
                self.assertLess(sig.stop, sig.target,
                                "Long: stop must be below target")
                return

    def test_short_signal_stop_above_target(self) -> None:
        bars = generate_synthetic_bars(count=800, seed=8)
        strategy = NYSessionBreakoutStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            sig = strategy.signal_for(bars, index)
            if sig is not None and sig.side is Side.SHORT:
                self.assertGreater(sig.stop, sig.target,
                                   "Short: stop must be above target")
                return

    def test_backtest_produces_valid_summary(self) -> None:
        bars = generate_synthetic_bars(count=800, seed=55)
        strategy = NYSessionBreakoutStrategy()
        result = run_backtest(bars, strategy, BacktestConfig())
        summary = summarize_backtest(result)
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertGreaterEqual(summary.win_rate, 0.0)
        self.assertLessEqual(summary.win_rate, 1.0)


class MomentumBurstStrategyTests(unittest.TestCase):
    def test_returns_correct_name(self) -> None:
        self.assertEqual(MomentumBurstStrategy().name, "momentum_burst")

    def test_warmup_bars_reasonable(self) -> None:
        strategy = MomentumBurstStrategy(atr_period=14)
        self.assertGreater(strategy.warmup_bars(), 14)

    def test_signal_for_does_not_raise_on_short_bars(self) -> None:
        bars = generate_synthetic_bars(count=50, seed=99)
        strategy = MomentumBurstStrategy()
        for index in range(len(bars)):
            strategy.signal_for(bars, index)

    def test_signal_for_returns_none_or_valid_signal(self) -> None:
        bars = generate_synthetic_bars(count=800, seed=42)
        strategy = MomentumBurstStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            result = strategy.signal_for(bars, index)
            if result is not None:
                self.assertIn(result.side.value, ("long", "short"))
                self.assertGreater(result.stop, 0.0)
                self.assertGreater(result.target, 0.0)

    def test_long_signal_stop_below_entry(self) -> None:
        bars = generate_synthetic_bars(count=1000, seed=13)
        strategy = MomentumBurstStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            sig = strategy.signal_for(bars, index)
            if sig is not None and sig.side is Side.LONG:
                self.assertLess(sig.stop, sig.target,
                                "Long: stop must be below target")
                return

    def test_short_signal_stop_above_entry(self) -> None:
        bars = generate_synthetic_bars(count=1000, seed=14)
        strategy = MomentumBurstStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            sig = strategy.signal_for(bars, index)
            if sig is not None and sig.side is Side.SHORT:
                self.assertGreater(sig.stop, sig.target,
                                   "Short: stop must be above target")
                return

    def test_backtest_produces_valid_summary(self) -> None:
        bars = generate_synthetic_bars(count=1000, seed=77)
        strategy = MomentumBurstStrategy()
        result = run_backtest(bars, strategy, BacktestConfig())
        summary = summarize_backtest(result)
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertGreaterEqual(summary.win_rate, 0.0)
        self.assertLessEqual(summary.win_rate, 1.0)
