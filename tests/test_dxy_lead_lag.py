"""Tests for DXYLeadLagStrategy."""
from __future__ import annotations

import unittest
from dataclasses import replace

from gold_trader.backtest.engine import BacktestConfig, run_backtest
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.models import MarketBar, Side
from gold_trader.strategies.dxy_lead_lag import DXYLeadLagStrategy


class DXYLeadLagStrategyTests(unittest.TestCase):
    def test_name(self) -> None:
        self.assertEqual(DXYLeadLagStrategy().name, "dxy_lead_lag")

    def test_warmup_bars_geq_atr_period(self) -> None:
        s = DXYLeadLagStrategy(atr_period=14, lookback=3)
        self.assertGreaterEqual(s.warmup_bars(), 14)

    def test_no_signal_before_warmup(self) -> None:
        bars = generate_synthetic_bars(count=50, seed=1)
        strategy = DXYLeadLagStrategy()
        for i in range(strategy.warmup_bars()):
            self.assertIsNone(strategy.signal_for(bars, i))

    def test_returns_none_when_dxy_close_missing(self) -> None:
        """Bars without dxy_close should never produce a signal."""
        bars = generate_synthetic_bars(count=100, seed=2)
        # Strip dxy_close from all bars.
        stripped = [replace(b, dxy_close=None) for b in bars]
        strategy = DXYLeadLagStrategy()
        for i in range(strategy.warmup_bars(), len(stripped)):
            self.assertIsNone(strategy.signal_for(stripped, i))

    def test_valid_signal_or_none_with_dxy_data(self) -> None:
        bars = generate_synthetic_bars(count=600, seed=42)
        strategy = DXYLeadLagStrategy()
        for i in range(strategy.warmup_bars(), len(bars)):
            sig = strategy.signal_for(bars, i)
            if sig is not None:
                self.assertIn(sig.side.value, ("long", "short"))
                self.assertGreater(sig.stop, 0.0)
                self.assertGreater(sig.target, 0.0)

    def test_long_signal_geometry(self) -> None:
        """Long signals must have stop < entry close and target > entry close."""
        bars = generate_synthetic_bars(count=1000, seed=10)
        strategy = DXYLeadLagStrategy()
        for i in range(strategy.warmup_bars(), len(bars)):
            sig = strategy.signal_for(bars, i)
            if sig is not None and sig.side is Side.LONG:
                bar_close = bars[i].close
                self.assertLess(sig.stop, bar_close,
                                "Long stop must be below close")
                self.assertGreater(sig.target, bar_close,
                                   "Long target must be above close")
                return
        self.skipTest("No long signal found in test data")

    def test_short_signal_geometry(self) -> None:
        """Short signals must have stop > entry close and target < entry close."""
        bars = generate_synthetic_bars(count=1000, seed=11)
        strategy = DXYLeadLagStrategy(min_dxy_drop=0.001)
        for i in range(strategy.warmup_bars(), len(bars)):
            sig = strategy.signal_for(bars, i)
            if sig is not None and sig.side is Side.SHORT:
                bar_close = bars[i].close
                self.assertGreater(sig.stop, bar_close,
                                   "Short stop must be above close")
                self.assertLess(sig.target, bar_close,
                                "Short target must be below close")
                return
        self.skipTest("No short signal found in test data")

    def test_backtest_runs_without_error(self) -> None:
        bars = generate_synthetic_bars(count=800, seed=55)
        strategy = DXYLeadLagStrategy()
        result = run_backtest(bars, strategy, BacktestConfig())
        self.assertIsNotNone(result)

    def test_no_signal_in_non_session(self) -> None:
        """Signals should be suppressed outside allowed sessions."""
        bars = generate_synthetic_bars(count=200, seed=7)
        # Relabel all bars as asian session.
        asian_bars = [replace(b, session="asian") for b in bars]
        strategy = DXYLeadLagStrategy(allowed_sessions=("london", "new_york"))
        for i in range(strategy.warmup_bars(), len(asian_bars)):
            self.assertIsNone(strategy.signal_for(asian_bars, i))

    def test_signal_fires_on_engineered_dxy_drop(self) -> None:
        """Manually engineer a DXY drop with no gold response and verify a LONG fires."""
        bars = generate_synthetic_bars(count=100, seed=3)
        # Set a flat DXY on warmup bars, then drop on the last bar.
        warmup = DXYLeadLagStrategy().warmup_bars()
        modified: list[MarketBar] = []
        for i, b in enumerate(bars[:warmup + 10]):
            if i < warmup:
                modified.append(replace(b, dxy_close=100.0, session="london"))
            elif i == warmup:
                # Sudden DXY drop, gold unchanged.
                modified.append(replace(b, dxy_close=99.5, session="london"))
            else:
                modified.append(replace(b, dxy_close=99.5, session="london"))

        strategy = DXYLeadLagStrategy(
            lookback=1,
            min_dxy_drop=0.004,   # 0.4%; the engineered drop is 0.5%
            max_gold_response=0.50,
            min_atr_threshold=0.0,
        )
        # The signal bar is right after the drop; gold delta ≈ 0 → response_fraction ≈ 0.
        found = False
        for i in range(strategy.warmup_bars(), len(modified)):
            sig = strategy.signal_for(modified, i)
            if sig is not None and sig.side is Side.LONG:
                found = True
                break
        self.assertTrue(found, "Expected a LONG signal after engineered DXY drop")
