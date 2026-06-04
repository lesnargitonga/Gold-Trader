"""Tests for RealYieldReversalStrategy.

Synthetic-bar tests (deterministic, no network) cover:
* Macro frame gating (no real10y data -> no signal)
* Direction logic (yield drop -> long; yield spike -> short)
* Once-per-day cap
* Cheap filters (session, spread)
* Warmup, geometry sanity
* Engine integration via run_backtest
"""
from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from gold_trader.backtest.engine import run_backtest
from gold_trader.data.macro import MacroFrame, MacroPoint, MacroSeries
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.models import BacktestConfig, MarketBar, Side
from gold_trader.strategies import RealYieldReversalStrategy


def _build_real10y(start_value: float, daily_deltas: list[float], start_date: datetime) -> MacroSeries:
    """Build a real10y MacroSeries from a start value and per-day deltas (in % points)."""
    pts = []
    value = start_value
    pts.append(MacroPoint(timestamp=start_date, value=value))
    for i, d in enumerate(daily_deltas, start=1):
        value += d
        pts.append(MacroPoint(timestamp=start_date + timedelta(days=i), value=value))
    return MacroSeries(name="real10y", source="fred", points=pts)


def _frame_with_drop() -> MacroFrame:
    """real10y drops 0.20 pct = 20 bps over 5 days."""
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    series = _build_real10y(2.00, [-0.04] * 60, start)
    return MacroFrame(series={"real10y": series})


def _frame_with_spike() -> MacroFrame:
    """real10y rises 0.20 pct = 20 bps over 5 days."""
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    series = _build_real10y(2.00, [+0.04] * 60, start)
    return MacroFrame(series={"real10y": series})


def _frame_flat() -> MacroFrame:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    series = _build_real10y(2.00, [0.0] * 60, start)
    return MacroFrame(series={"real10y": series})


def _bars_in_jan(count: int = 240, seed: int = 1) -> list[MarketBar]:
    """Synthetic 60m bars rebased to start 2025-01-10 09:00 UTC."""
    raw = generate_synthetic_bars(count=count, seed=seed)
    base = datetime(2025, 1, 10, 9, 0, tzinfo=timezone.utc)
    out: list[MarketBar] = []
    for i, b in enumerate(raw):
        ts = base + timedelta(hours=i)
        # Tag london/new_york session windows so the strategy gate passes.
        h = ts.hour
        if 7 <= h < 13:
            session = "london"
        elif 13 <= h < 21:
            session = "new_york"
        else:
            session = "asia"
        out.append(replace(b, timestamp=ts, session=session, spread=0.3))
    return out


class WarmupAndContractTests(unittest.TestCase):
    def test_name(self) -> None:
        s = RealYieldReversalStrategy(macro=_frame_flat())
        self.assertEqual(s.name, "real_yield_reversal")

    def test_warmup_geq_atr(self) -> None:
        s = RealYieldReversalStrategy(macro=_frame_flat(), atr_period=14)
        self.assertGreaterEqual(s.warmup_bars(), 14)

    def test_no_signal_when_macro_missing(self) -> None:
        empty = MacroFrame(series={})
        s = RealYieldReversalStrategy(macro=empty)
        bars = _bars_in_jan()
        for i in range(s.warmup_bars(), len(bars)):
            self.assertIsNone(s.signal_for(bars, i))

    def test_no_signal_in_flat_yield_regime(self) -> None:
        s = RealYieldReversalStrategy(macro=_frame_flat(), min_yield_move_bps=10.0)
        bars = _bars_in_jan()
        sigs = [s.signal_for(bars, i) for i in range(s.warmup_bars(), len(bars))]
        self.assertTrue(all(x is None for x in sigs))


class DirectionTests(unittest.TestCase):
    def test_yield_drop_produces_long(self) -> None:
        s = RealYieldReversalStrategy(macro=_frame_with_drop(), min_yield_move_bps=10.0)
        bars = _bars_in_jan()
        signals = [(i, s.signal_for(bars, i)) for i in range(s.warmup_bars(), len(bars))]
        non_null = [(i, sig) for i, sig in signals if sig is not None]
        self.assertGreater(len(non_null), 0, "Expected at least one signal in 5-day yield-drop regime")
        for _, sig in non_null:
            self.assertIs(sig.side, Side.LONG)

    def test_yield_spike_produces_short(self) -> None:
        s = RealYieldReversalStrategy(macro=_frame_with_spike(), min_yield_move_bps=10.0)
        bars = _bars_in_jan()
        signals = [(i, s.signal_for(bars, i)) for i in range(s.warmup_bars(), len(bars))]
        non_null = [(i, sig) for i, sig in signals if sig is not None]
        self.assertGreater(len(non_null), 0)
        for _, sig in non_null:
            self.assertIs(sig.side, Side.SHORT)

    def test_enter_longs_false_suppresses_long(self) -> None:
        s = RealYieldReversalStrategy(
            macro=_frame_with_drop(), min_yield_move_bps=10.0, enter_longs=False
        )
        bars = _bars_in_jan()
        for i in range(s.warmup_bars(), len(bars)):
            self.assertIsNone(s.signal_for(bars, i))


class GeometryTests(unittest.TestCase):
    def test_long_geometry(self) -> None:
        s = RealYieldReversalStrategy(macro=_frame_with_drop())
        bars = _bars_in_jan()
        for i in range(s.warmup_bars(), len(bars)):
            sig = s.signal_for(bars, i)
            if sig is not None and sig.side is Side.LONG:
                self.assertLess(sig.stop, bars[i].close)
                self.assertGreater(sig.target, bars[i].close)
                self.assertGreater(sig.risk_reward, 0.0)
                return
        self.fail("No long signal found")

    def test_short_geometry(self) -> None:
        s = RealYieldReversalStrategy(macro=_frame_with_spike())
        bars = _bars_in_jan()
        for i in range(s.warmup_bars(), len(bars)):
            sig = s.signal_for(bars, i)
            if sig is not None and sig.side is Side.SHORT:
                self.assertGreater(sig.stop, bars[i].close)
                self.assertLess(sig.target, bars[i].close)
                return
        self.fail("No short signal found")


class OncePerDayTests(unittest.TestCase):
    def test_at_most_one_signal_per_utc_date(self) -> None:
        s = RealYieldReversalStrategy(macro=_frame_with_drop(), min_yield_move_bps=10.0)
        bars = _bars_in_jan(count=240)
        signals_by_date: dict = {}
        for i in range(s.warmup_bars(), len(bars)):
            sig = s.signal_for(bars, i)
            if sig is None:
                continue
            d = bars[i].timestamp.astimezone(timezone.utc).date()
            signals_by_date.setdefault(d, []).append(i)
        for d, indices in signals_by_date.items():
            self.assertEqual(len(indices), 1, f"Multiple signals on {d}: {indices}")


class FilterTests(unittest.TestCase):
    def test_spread_filter_blocks_signal(self) -> None:
        s = RealYieldReversalStrategy(
            macro=_frame_with_drop(), max_spread=0.5
        )
        bars = _bars_in_jan()
        # Push spread above max_spread on every bar.
        wide = [replace(b, spread=2.0) for b in bars]
        for i in range(s.warmup_bars(), len(wide)):
            self.assertIsNone(s.signal_for(wide, i))

    def test_session_filter_blocks_signal(self) -> None:
        s = RealYieldReversalStrategy(
            macro=_frame_with_drop(), allowed_sessions=("foo",)
        )
        bars = _bars_in_jan()
        for i in range(s.warmup_bars(), len(bars)):
            self.assertIsNone(s.signal_for(bars, i))


class EngineIntegrationTests(unittest.TestCase):
    def test_run_backtest_completes_without_error(self) -> None:
        s = RealYieldReversalStrategy(macro=_frame_with_drop(), min_yield_move_bps=10.0)
        bars = _bars_in_jan(count=480)
        # Disable kill-switch — synthetic random-walk price + forced longs every
        # day will naturally cluster losses and trip the 4% daily DD circuit.
        # That is correct engine behaviour; here we only verify the integration
        # path is sound.
        cfg = BacktestConfig(kill_switch_drawdown_fraction=None)
        result = run_backtest(bars, s, cfg)
        self.assertEqual(result.strategy_name, "real_yield_reversal")


if __name__ == "__main__":
    unittest.main()
