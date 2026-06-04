from __future__ import annotations

import unittest

from gold_trader.backtest import summarize_backtest
from gold_trader.backtest.engine import run_backtest
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.models import BacktestConfig
from gold_trader.strategies import LiquiditySweepStrategy
from gold_trader.validation import build_walk_forward_windows


class SmokeBacktestTests(unittest.TestCase):
    def test_liquidity_sweep_strategy_produces_closed_trades(self) -> None:
        bars = generate_synthetic_bars(count=500, seed=11)
        strategy = LiquiditySweepStrategy()
        result = run_backtest(bars, strategy, BacktestConfig())
        summary = summarize_backtest(result)

        self.assertGreater(summary.total_trades, 0)
        self.assertGreater(summary.ending_equity, 0.0)
        self.assertGreaterEqual(summary.max_drawdown, 0.0)

    def test_walk_forward_window_generation(self) -> None:
        windows = build_walk_forward_windows(
            total_bars=1_000,
            train_size=300,
            test_size=100,
            step_size=100,
        )

        self.assertEqual(len(windows), 7)
        self.assertEqual(windows[0].test_start, 300)
        self.assertEqual(windows[-1].test_end, 1_000)


if __name__ == "__main__":
    unittest.main()