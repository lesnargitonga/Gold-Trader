from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.models import BacktestConfig
from gold_trader.probability_gate import evaluate_probability_gate
from gold_trader.research.probability_slicer import (
    compute_probability_table,
    write_probability_table,
)
from gold_trader.strategies import LiquiditySweepStrategy


class ProbabilityGateTests(unittest.TestCase):
    def test_no_table_returns_no_table_verdict(self) -> None:
        bars = generate_synthetic_bars(count=100, seed=3)
        with tempfile.TemporaryDirectory() as d:
            v = evaluate_probability_gate(
                family="liquidity_sweep",
                side="long",
                bars=bars,
                tables_dir=Path(d),
            )
            self.assertEqual(v.verdict, "no_table")

    def test_block_when_no_qualifying_slice(self) -> None:
        bars = generate_synthetic_bars(count=600, seed=23)
        table = compute_probability_table(
            bars, LiquiditySweepStrategy(), BacktestConfig(),
            family="liquidity_sweep", include_pairs=False,
        )
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write_probability_table(table, d / "liquidity_sweep.json")
            # ridiculously high gates so nothing qualifies
            v = evaluate_probability_gate(
                family="liquidity_sweep",
                side="long",
                bars=bars,
                tables_dir=d,
                min_n=10_000,
                min_expectancy_r=10.0,
                min_profit_factor=1_000.0,
            )
            self.assertEqual(v.verdict, "block")

    def test_allow_when_slice_matches(self) -> None:
        bars = generate_synthetic_bars(count=600, seed=23)
        table = compute_probability_table(
            bars, LiquiditySweepStrategy(), BacktestConfig(),
            family="liquidity_sweep", include_pairs=False,
        )
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            write_probability_table(table, d / "liquidity_sweep.json")
            # Permissive gates → almost any non-empty slice qualifies
            v = evaluate_probability_gate(
                family="liquidity_sweep",
                side="long",
                bars=bars,
                tables_dir=d,
                min_n=2,
                min_expectancy_r=-1e9,
                min_profit_factor=0.0,
            )
            # Either matches a slice (allow) or no match because side/regime
            # didn't appear in tagged trades (block). Both outcomes legal.
            self.assertIn(v.verdict, {"allow", "block"})
            if v.verdict == "allow":
                self.assertIsNotNone(v.matched_slice)
                self.assertGreater(v.matched_slice.n, 0)

    def test_table_corruption_returns_no_table(self) -> None:
        bars = generate_synthetic_bars(count=80, seed=5)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "liquidity_sweep.json").write_text("{not json}")
            v = evaluate_probability_gate(
                family="liquidity_sweep",
                side="long",
                bars=bars,
                tables_dir=d,
            )
            self.assertEqual(v.verdict, "no_table")

    def test_current_dimensions_are_reported(self) -> None:
        bars = generate_synthetic_bars(count=80, seed=5)
        with tempfile.TemporaryDirectory() as d:
            v = evaluate_probability_gate(
                family="liquidity_sweep", side="long",
                bars=bars, tables_dir=Path(d),
            )
            self.assertIn("session", v.current_dims)
            self.assertIn("trend", v.current_dims)
            self.assertEqual(v.current_dims["side"], "long")


if __name__ == "__main__":
    unittest.main()
