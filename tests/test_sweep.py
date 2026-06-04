from __future__ import annotations

import unittest

from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.models import BacktestConfig
from gold_trader.research import build_liquidity_sweep_grid, run_liquidity_sweep_sweep


class ParameterSweepTests(unittest.TestCase):
    def test_parameter_sweep_returns_ranked_results(self) -> None:
        bars = generate_synthetic_bars(count=500, seed=13)
        parameter_grid = build_liquidity_sweep_grid(
            lookbacks=[15, 20],
            atr_periods=[14],
            min_sweep_atrs=[0.2, 0.3],
            risk_rewards=[1.5, 2.0],
            max_spreads=[0.35],
            min_news_distances=[30.0],
        )

        results = run_liquidity_sweep_sweep(
            bars=bars,
            config=BacktestConfig(),
            parameter_grid=parameter_grid,
            max_workers=1,
        )

        self.assertEqual(len(results), 8)
        self.assertGreaterEqual(results[0].summary.average_r, results[-1].summary.average_r)


if __name__ == "__main__":
    unittest.main()