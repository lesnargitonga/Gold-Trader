from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.models import BacktestConfig
from gold_trader.research.probability_slicer import (
    DEFAULT_DIMENSIONS,
    compute_probability_table,
    lookup_slice_probability,
    write_probability_table,
)
from gold_trader.strategies import LiquiditySweepStrategy


def _table():
    bars = generate_synthetic_bars(count=600, seed=23)
    strategy = LiquiditySweepStrategy()
    return bars, compute_probability_table(
        bars,
        strategy,
        BacktestConfig(),
        family="liquidity_sweep",
        include_pairs=True,
        min_pair_n=4,
    )


class ProbabilitySlicerTests(unittest.TestCase):
    def test_table_has_trades_and_slices(self) -> None:
        _, table = _table()
        self.assertEqual(table.family, "liquidity_sweep")
        self.assertGreater(table.n_total, 0)
        # at least some single-dim slices computed
        self.assertGreater(len(table.single_slices), 0)
        # every slice's value-count matches its dimension-count
        for s in (*table.single_slices, *table.pair_slices):
            self.assertEqual(len(s.dimensions), len(s.values))
            self.assertGreaterEqual(s.n, 2)
            self.assertLessEqual(s.wins + s.losses, s.n)

    def test_base_stats_are_consistent(self) -> None:
        _, table = _table()
        self.assertGreaterEqual(table.base_win_rate, 0.0)
        self.assertLessEqual(table.base_win_rate, 1.0)
        self.assertGreaterEqual(table.base_profit_factor, 0.0)

    def test_serialisation_roundtrip(self) -> None:
        _, table = _table()
        with tempfile.TemporaryDirectory() as d:
            p = write_probability_table(table, Path(d) / "foo.json")
            blob = json.loads(p.read_text())
            self.assertEqual(blob["family"], "liquidity_sweep")
            self.assertEqual(blob["n_total"], table.n_total)
            self.assertEqual(len(blob["single_slices"]), len(table.single_slices))

    def test_edge_slices_respect_gates(self) -> None:
        _, table = _table()
        edges = table.edge_slices(min_n=5, min_expectancy_r=0.0, min_profit_factor=1.0)
        for s in edges:
            self.assertGreaterEqual(s.n, 5)
            self.assertGreaterEqual(s.expectancy, 0.0)
            self.assertGreaterEqual(s.profit_factor, 1.0)

    def test_lookup_returns_specific_slice_when_available(self) -> None:
        _, table = _table()
        # Pick any slice with healthy stats and try to look it up
        candidate = None
        for s in table.single_slices:
            if s.n >= 5 and s.profit_factor >= 1.0:
                candidate = s
                break
        if candidate is None:
            self.skipTest("synthetic data produced no qualifying slice")
        current = {candidate.dimensions[0]: candidate.values[0]}
        hit = lookup_slice_probability(
            table, current,
            min_n=5, min_expectancy_r=-1e9, min_profit_factor=1.0,
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.dimensions[0], candidate.dimensions[0])

    def test_lookup_returns_none_when_no_match(self) -> None:
        _, table = _table()
        current = {"session": "__nonexistent__"}
        hit = lookup_slice_probability(
            table, current, min_n=5, min_expectancy_r=0.0, min_profit_factor=1.0,
        )
        self.assertIsNone(hit)

    def test_default_dimensions_are_present(self) -> None:
        self.assertIn("session", DEFAULT_DIMENSIONS)
        self.assertIn("trend", DEFAULT_DIMENSIONS)
        self.assertIn("side", DEFAULT_DIMENSIONS)


if __name__ == "__main__":
    unittest.main()
