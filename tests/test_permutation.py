from __future__ import annotations

import unittest

from gold_trader.backtest.engine import BacktestConfig
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.research.permutation import PermutationTestResult, run_permutation_test
from gold_trader.strategies import LiquiditySweepStrategy


class PermutationTestBasicTests(unittest.TestCase):
    def test_returns_permutation_test_result(self) -> None:
        bars = generate_synthetic_bars(count=500, seed=42)
        strategy = LiquiditySweepStrategy()
        config = BacktestConfig()
        result = run_permutation_test(bars, strategy, config, n_permutations=200, seed=0)
        self.assertIsInstance(result, PermutationTestResult)

    def test_p_value_between_0_and_1(self) -> None:
        bars = generate_synthetic_bars(count=500, seed=99)
        strategy = LiquiditySweepStrategy()
        config = BacktestConfig()
        result = run_permutation_test(bars, strategy, config, n_permutations=100, seed=1)
        self.assertGreaterEqual(result.p_value, 0.0)
        self.assertLessEqual(result.p_value, 1.0)

    def test_percentile_rank_in_valid_range(self) -> None:
        bars = generate_synthetic_bars(count=300, seed=7)
        strategy = LiquiditySweepStrategy()
        config = BacktestConfig()
        result = run_permutation_test(bars, strategy, config, n_permutations=100, seed=5)
        self.assertGreaterEqual(result.percentile_rank, 0.0)
        self.assertLessEqual(result.percentile_rank, 100.0)

    def test_verdict_is_one_of_expected_strings(self) -> None:
        bars = generate_synthetic_bars(count=500, seed=3)
        strategy = LiquiditySweepStrategy()
        config = BacktestConfig()
        result = run_permutation_test(bars, strategy, config, n_permutations=100, seed=2)
        valid_prefixes = ("SIGNAL", "WEAK SIGNAL", "MARGINAL", "NOISE", "INCONCLUSIVE")
        self.assertTrue(
            any(result.verdict.startswith(p) for p in valid_prefixes),
            f"Unexpected verdict: {result.verdict!r}",
        )

    def test_result_is_deterministic_across_same_seed(self) -> None:
        bars = generate_synthetic_bars(count=400, seed=10)
        strategy = LiquiditySweepStrategy()
        config = BacktestConfig()
        r1 = run_permutation_test(bars, strategy, config, n_permutations=500, seed=42)
        r2 = run_permutation_test(bars, strategy, config, n_permutations=500, seed=42)
        self.assertAlmostEqual(r1.p_value, r2.p_value, places=8)
        self.assertEqual(r1.n_trades, r2.n_trades)

    def test_zero_trades_produces_inconclusive_verdict(self) -> None:
        """When there are no trades the test should gracefully return INCONCLUSIVE."""
        # Use minimum-length bars that won't generate sweep signals (no real price pattern)
        bars = generate_synthetic_bars(count=50, seed=0)
        strategy = LiquiditySweepStrategy()
        config = BacktestConfig()
        result = run_permutation_test(bars, strategy, config, n_permutations=50, seed=0)
        # Either zero trades → INCONCLUSIVE, or some trades with NOISE verdict
        if result.n_trades == 0:
            self.assertTrue(result.verdict.startswith("INCONCLUSIVE"))
        else:
            self.assertTrue(
                any(result.verdict.startswith(p) for p in ("SIGNAL", "WEAK SIGNAL", "MARGINAL", "NOISE"))
            )


if __name__ == "__main__":
    unittest.main()
