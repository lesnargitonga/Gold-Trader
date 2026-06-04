from __future__ import annotations

import unittest

from gold_trader.data.dukascopy import resample_bars
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.models import BacktestConfig
from gold_trader.research import (
    build_compression_breakout_grid,
    build_liquidity_sweep_grid,
    run_research_bundle,
)
from gold_trader.validation import summarize_walk_forward


class ResearchBundleTests(unittest.TestCase):
    def test_research_bundle_returns_ranked_results(self) -> None:
        bars_15 = generate_synthetic_bars(count=500, seed=23)
        bars_60 = resample_bars(bars_15, interval_minutes=60)
        datasets = {15: bars_15, 60: bars_60}

        results = run_research_bundle(
            datasets=datasets,
            config=BacktestConfig(),
            families=["liquidity_sweep", "compression_breakout"],
            liquidity_grid=build_liquidity_sweep_grid(
                lookbacks=[15],
                atr_periods=[14],
                min_sweep_atrs=[0.2],
                risk_rewards=[2.0],
                max_spreads=[0.75],
                min_news_distances=[0.0],
            ),
            compression_grid=build_compression_breakout_grid(
                breakout_lookbacks=[8],
                compression_lookbacks=[4],
                atr_periods=[14],
                max_compression_atr_ratios=[1.0],
                min_breakout_atrs=[0.1],
                risk_rewards=[2.0],
                max_spreads=[0.75],
                min_news_distances=[0.0],
            ),
            train_bars=80,
            test_bars=40,
            step_bars=40,
            min_trades=0,
            max_workers=1,
        )

        self.assertEqual(len(results), 4)
        self.assertIn(results[0].family, {"liquidity_sweep", "compression_breakout"})
        self.assertGreaterEqual(results[0].walk_forward.window_count, 0)

    def test_summarize_walk_forward_handles_empty_results(self) -> None:
        summary = summarize_walk_forward([])

        self.assertEqual(summary.window_count, 0)
        self.assertEqual(summary.positive_window_ratio, 0.0)


if __name__ == "__main__":
    unittest.main()