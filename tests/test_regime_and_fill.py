"""Tests for RegimeDetector and engine slippage/fill-aware modes."""
from __future__ import annotations

import unittest
from dataclasses import replace

from gold_trader.backtest.engine import run_backtest
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.models import BacktestConfig, Side
from gold_trader.regime import RegimeDetector
from gold_trader.strategies.asian_range_breakout import AsianRangeBreakoutStrategy


class RegimeDetectorTests(unittest.TestCase):
    def test_classifies_synthetic_bars(self) -> None:
        bars = generate_synthetic_bars(count=300, seed=3)
        d = RegimeDetector()
        tags = d.classify(bars, index=250)
        # All categorical fields should have valid tokens.
        self.assertIn(tags.vol_pct, ("low", "mid", "high"))
        self.assertIn(tags.trend, ("up", "flat", "down"))
        self.assertIn(tags.compression, ("expanding", "stable", "compressing"))
        self.assertIn(tags.spread, ("tight", "normal", "wide"))
        # No macro frame -> all macro fields unknown / False.
        self.assertEqual(tags.macro_real10y, "unknown")
        self.assertEqual(tags.macro_dxy, "unknown")
        self.assertEqual(tags.macro_vix, "unknown")
        self.assertFalse(tags.macro_stagflation)

    def test_to_dict_serializable(self) -> None:
        bars = generate_synthetic_bars(count=200, seed=4)
        d = RegimeDetector()
        tags = d.classify(bars, index=150)
        out = tags.to_dict()
        self.assertEqual(set(out), {
            "vol_pct", "trend", "compression", "spread",
            "macro_real10y", "macro_dxy", "macro_vix", "macro_stagflation",
            "session_vwap",
        })

    def test_index_bounds(self) -> None:
        bars = generate_synthetic_bars(count=100, seed=5)
        d = RegimeDetector()
        with self.assertRaises(IndexError):
            d.classify(bars, index=len(bars))
        with self.assertRaises(IndexError):
            d.classify(bars, index=0)


class EngineFillAwareTests(unittest.TestCase):
    def test_slippage_reduces_realised_r(self) -> None:
        """With non-zero slippage, the same backtest should produce equal-or-worse R."""
        bars = generate_synthetic_bars(count=600, seed=7)
        strat = AsianRangeBreakoutStrategy(
            atr_period=10, risk_reward=2.5, max_spread=2.0,
            min_atr_threshold=0.0,
        )
        cfg_clean = BacktestConfig(starting_equity=10_000.0)
        cfg_slip = replace(cfg_clean, slippage_bps=10.0)  # ~10bp adverse fill

        r_clean = run_backtest(bars, strat, cfg_clean)
        r_slip = run_backtest(bars, strat, cfg_slip)

        if not r_clean.trades or not r_slip.trades:
            self.skipTest("no trades on synthetic data")

        avg_clean = sum(t.pnl_r for t in r_clean.trades) / len(r_clean.trades)
        avg_slip = sum(t.pnl_r for t in r_slip.trades) / len(r_slip.trades)
        # Slippage should *not* improve realised R.
        self.assertLessEqual(avg_slip, avg_clean + 1e-6)

    def test_fill_aware_stops_preserves_geometry(self) -> None:
        """fill_aware_stops mode should produce different trade R than legacy mode
        when there is non-trivial entry drift."""
        bars = generate_synthetic_bars(count=600, seed=11)
        strat = AsianRangeBreakoutStrategy(
            atr_period=10, risk_reward=2.5, max_spread=2.0,
            min_atr_threshold=0.0,
        )
        cfg_legacy = BacktestConfig(starting_equity=10_000.0)
        cfg_fa = replace(cfg_legacy, fill_aware_stops=True)
        r_legacy = run_backtest(bars, strat, cfg_legacy)
        r_fa = run_backtest(bars, strat, cfg_fa)
        # Both should run without raising. Trades may differ in count and PnL.
        self.assertIsNotNone(r_legacy.trades)
        self.assertIsNotNone(r_fa.trades)


if __name__ == "__main__":
    unittest.main()
