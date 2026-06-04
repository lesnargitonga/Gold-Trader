from __future__ import annotations

import unittest

from gold_trader.data.dukascopy import resample_bars
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.research import build_bundle_snapshot


class StateSnapshotTests(unittest.TestCase):
    def test_build_bundle_snapshot_returns_state_and_bias(self) -> None:
        bars_5 = generate_synthetic_bars(count=500, seed=53)
        bars_15 = resample_bars(bars_5, interval_minutes=15)
        bars_60 = resample_bars(bars_5, interval_minutes=60)

        snapshot = build_bundle_snapshot(
            datasets={5: bars_5, 15: bars_15, 60: bars_60},
            families=["liquidity_sweep", "compression_breakout"],
            max_candidates=5,
        )

        self.assertEqual(len(snapshot.timeframe_states), 3)
        self.assertIn(snapshot.higher_timeframe_bias, {"bullish", "bearish", "neutral"})
        self.assertIn(
            snapshot.oscillation_label,
            {"oscillating mean-reversion regime", "trend / breakout regime", "mixed transition regime"},
        )
        self.assertLessEqual(len(snapshot.entry_candidates), 5)
        self.assertIn(snapshot.decision.status, {"accept", "hold", "reject"})
        self.assertGreaterEqual(len(snapshot.decision.rationale), 1)


if __name__ == "__main__":
    unittest.main()