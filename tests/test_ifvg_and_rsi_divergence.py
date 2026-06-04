"""Tests for InversionFairValueGap and RsiDivergence strategies."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from gold_trader.models import MarketBar, Side
from gold_trader.strategies.inversion_fair_value_gap import InversionFairValueGapStrategy
from gold_trader.strategies.rsi_divergence import RsiDivergenceStrategy


def _bar(ts: datetime, o: float, h: float, l: float, c: float, *, spread: float = 0.3, session: str = "new_york") -> MarketBar:
    return MarketBar(timestamp=ts, open=o, high=h, low=l, close=c, volume=100.0, spread=spread, session=session)


def _make_series(opens_highs_lows_closes: list[tuple[float, float, float, float]],
                 *, start: datetime | None = None,
                 step: timedelta = timedelta(minutes=15),
                 session: str = "new_york") -> list[MarketBar]:
    start = start or datetime(2025, 6, 2, 13, 0, tzinfo=timezone.utc)  # Mon NY session
    bars: list[MarketBar] = []
    ts = start
    for (o, h, l, c) in opens_highs_lows_closes:
        bars.append(_bar(ts, o, h, l, c, session=session))
        ts = ts + step
    return bars


# ─────────────────────────────────────────────────────────────────────────────
# Inversion FVG
# ─────────────────────────────────────────────────────────────────────────────

class InversionFairValueGapTests(unittest.TestCase):

    def test_warmup_bars(self) -> None:
        s = InversionFairValueGapStrategy()
        self.assertGreaterEqual(s.warmup_bars(), s.atr_period + s.fvg_lookback)

    def test_no_signal_on_random_walk(self) -> None:
        s = InversionFairValueGapStrategy()
        # Flat noise — no FVG should form, no signal
        bars = _make_series([(2400.0, 2401.0, 2399.0, 2400.5)] * 80)
        for i in range(s.warmup_bars(), len(bars)):
            self.assertIsNone(s.signal_for(bars, i))

    def test_bullish_fvg_inverts_to_short(self) -> None:
        """Construct: 60 calm bars → bullish 3-bar FVG → drop through it (inversion)
        → retest the inverted zone from below → SHORT signal."""
        s = InversionFairValueGapStrategy(
            atr_period=10, min_gap_atr=0.05, fvg_lookback=20,
            inversion_lookback=15, retest_lookback=5,
            filters_enabled=(),
        )
        ohlc: list[tuple[float, float, float, float]] = []
        # 30 calm bars
        for _ in range(30):
            ohlc.append((2400.0, 2401.0, 2399.0, 2400.0))
        # Bullish FVG: bar k-2 high=2401, k-1 large impulse up, k low=2410 (gap 9)
        ohlc.append((2400.0, 2402.0, 2399.0, 2401.0))   # k-2: high 2402
        ohlc.append((2402.0, 2412.0, 2401.5, 2411.0))   # k-1: impulse
        ohlc.append((2412.0, 2415.0, 2410.5, 2413.0))   # k: low 2410.5 -> gap (2402, 2410.5)
        # 4 bars holding above FVG
        for _ in range(4):
            ohlc.append((2413.0, 2414.0, 2412.0, 2413.0))
        # Now invert: drop a bar that closes below 2402
        ohlc.append((2412.0, 2413.0, 2400.0, 2400.5))   # close 2400.5 < 2402 -> inverted
        # Retest from below: pierce 2402 then close back below
        ohlc.append((2400.5, 2402.5, 2400.0, 2401.0))   # high 2402.5 >= 2402, close 2401 <= 2402

        bars = _make_series(ohlc)
        sig = s.signal_for(bars, len(bars) - 1)
        self.assertIsNotNone(sig, "Expected SHORT IFVG signal on retest of inverted bullish gap")
        self.assertEqual(sig.side, Side.SHORT)
        self.assertGreater(sig.stop, 2402.0)  # stop above inverted zone top
        self.assertLess(sig.target, sig.stop)  # short geometry
        self.assertIn("ifvg", sig.tags)

    def test_bearish_fvg_inverts_to_long(self) -> None:
        s = InversionFairValueGapStrategy(
            atr_period=10, min_gap_atr=0.05, fvg_lookback=20,
            inversion_lookback=15, retest_lookback=5,
            filters_enabled=(),
        )
        ohlc: list[tuple[float, float, float, float]] = []
        for _ in range(30):
            ohlc.append((2400.0, 2401.0, 2399.0, 2400.0))
        # Bearish FVG: k-2 low=2399, k-1 impulse down, k high=2390 -> gap (2390, 2399)
        ohlc.append((2400.0, 2401.0, 2399.0, 2399.5))   # k-2: low 2399
        ohlc.append((2399.0, 2399.5, 2389.0, 2390.0))   # k-1: impulse down
        ohlc.append((2389.0, 2389.5, 2385.0, 2387.0))   # k: high 2389.5 -> gap (2389.5, 2399)
        for _ in range(4):
            ohlc.append((2387.0, 2388.0, 2386.0, 2387.0))
        # Invert upward: close above 2399
        ohlc.append((2388.0, 2400.0, 2387.5, 2399.5))   # close 2399.5 > 2399 -> inverted
        # Retest from above: dip into zone, close back above
        ohlc.append((2399.5, 2400.0, 2398.5, 2399.5))   # low 2398.5 <= 2399, close 2399.5 >= 2399

        bars = _make_series(ohlc)
        sig = s.signal_for(bars, len(bars) - 1)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, Side.LONG)
        self.assertLess(sig.stop, 2389.5)
        self.assertGreater(sig.target, sig.stop)
        self.assertIn("ifvg", sig.tags)

    def test_high_spread_blocks_signal(self) -> None:
        s = InversionFairValueGapStrategy(max_spread=0.50, min_gap_atr=0.05, fvg_lookback=20)
        bars = _make_series([(2400.0, 2401.0, 2399.0, 2400.0)] * 60)
        # Replace last bar with high-spread bar
        last = bars[-1]
        bars[-1] = MarketBar(
            timestamp=last.timestamp, open=last.open, high=last.high,
            low=last.low, close=last.close, volume=last.volume,
            spread=2.0, session=last.session,
        )
        self.assertIsNone(s.signal_for(bars, len(bars) - 1))


# ─────────────────────────────────────────────────────────────────────────────
# RSI Divergence
# ─────────────────────────────────────────────────────────────────────────────

class RsiDivergenceTests(unittest.TestCase):

    def test_warmup_bars(self) -> None:
        s = RsiDivergenceStrategy()
        self.assertGreater(s.warmup_bars(), s.rsi_period)

    def test_no_signal_on_flat(self) -> None:
        s = RsiDivergenceStrategy()
        bars = _make_series([(2400.0, 2400.5, 2399.5, 2400.0)] * 80)
        for i in range(s.warmup_bars(), len(bars)):
            self.assertIsNone(s.signal_for(bars, i))

    def test_bullish_divergence_with_hammer_emits_long(self) -> None:
        s = RsiDivergenceStrategy(
            rsi_period=10, atr_period=10, oversold=35.0,
            pivot_window=2, pivot_lookback=30, min_pivot_separation=4,
            filters_enabled=(),
        )
        # Construct: deep drop -> pivot low A (RSI very low),
        # bounce, second drop to a LOWER low B but with smaller momentum
        # so RSI prints HIGHER than at A. Then a hammer reversal candle.
        ohlc: list[tuple[float, float, float, float]] = []
        # 50 baseline bars (need warmup ~ rsi+pivot_lookback+window+2 ≈ 44)
        for _ in range(50):
            ohlc.append((2400.0, 2400.8, 2399.2, 2400.0))
        # Strong drop to pivot low A — bar A has unique low=2380
        ohlc.append((2400.0, 2400.0, 2393.0, 2393.0))
        ohlc.append((2393.0, 2393.0, 2387.0, 2387.0))
        ohlc.append((2387.0, 2387.0, 2380.0, 2381.0))   # pivot-low A: low=2380
        ohlc.append((2381.0, 2384.0, 2381.0, 2383.0))
        ohlc.append((2383.0, 2386.0, 2383.0, 2385.0))
        # Bounce up
        for c in (2387.0, 2389.0, 2391.0, 2392.0, 2391.5, 2390.0, 2389.0):
            ohlc.append((c - 0.5, c + 0.3, c - 0.7, c))
        # Slow second drop to a lower low B — make B uniquely lowest
        for c in (2388.0, 2386.0, 2384.0, 2382.0, 2380.5, 2379.5):
            ohlc.append((c + 0.3, c + 0.5, c - 0.5, c))
        # Pivot-low B: unique low=2377.5, neighbors strictly higher
        ohlc.append((2379.0, 2379.5, 2377.5, 2378.5))   # B (low=2377.5)
        ohlc.append((2378.5, 2379.5, 2378.0, 2379.0))
        ohlc.append((2379.0, 2380.5, 2378.7, 2380.0))
        # 2 more bars to confirm pivot (window=2)
        ohlc.append((2380.0, 2381.5, 2379.5, 2381.0))
        # Hammer reversal candle
        ohlc.append((2380.5, 2381.0, 2376.0, 2380.8))   # body 0.3, lower wick 4.5

        bars = _make_series(ohlc)
        sig = None
        for i in range(len(bars) - 1, len(bars) - 5, -1):
            sig = s.signal_for(bars, i)
            if sig is not None:
                break
        self.assertIsNotNone(sig, "Expected LONG RSI bullish divergence signal")
        self.assertEqual(sig.side, Side.LONG)
        self.assertLess(sig.stop, bars[-1].close)
        self.assertGreater(sig.target, sig.stop)
        self.assertIn("rsi_div", sig.tags)

    def test_rsi_period_correctness(self) -> None:
        """Wilder's RSI on a known monotonically rising series → high RSI."""
        s = RsiDivergenceStrategy(rsi_period=14)
        # 20 bars rising linearly
        ohlc = [(2400 + i, 2400.5 + i, 2399.5 + i, 2400 + i + 0.5) for i in range(20)]
        bars = _make_series(ohlc)
        rsi = s._rsi_series(bars, len(bars) - 1)
        # Last RSI should be near 100 since all losses are zero
        self.assertGreater(rsi[-1], 95.0)

    def test_bullish_engulfing_detection(self) -> None:
        prev = _bar(datetime(2025, 6, 2, 12, 0, tzinfo=timezone.utc), 2402.0, 2402.5, 2399.5, 2399.5)
        bar = _bar(datetime(2025, 6, 2, 12, 15, tzinfo=timezone.utc), 2399.0, 2403.5, 2398.5, 2403.0)
        self.assertTrue(RsiDivergenceStrategy._is_bullish_reversal_candle(prev, bar))

    def test_bearish_engulfing_detection(self) -> None:
        prev = _bar(datetime(2025, 6, 2, 12, 0, tzinfo=timezone.utc), 2398.0, 2401.5, 2398.0, 2401.0)
        bar = _bar(datetime(2025, 6, 2, 12, 15, tzinfo=timezone.utc), 2402.0, 2403.0, 2397.5, 2398.0)
        self.assertTrue(RsiDivergenceStrategy._is_bearish_reversal_candle(prev, bar))

    def test_high_spread_blocks_signal(self) -> None:
        s = RsiDivergenceStrategy(max_spread=0.50)
        bars = _make_series([(2400.0, 2400.5, 2399.5, 2400.0)] * 80)
        last = bars[-1]
        bars[-1] = MarketBar(
            timestamp=last.timestamp, open=last.open, high=last.high,
            low=last.low, close=last.close, volume=last.volume,
            spread=2.0, session=last.session,
        )
        self.assertIsNone(s.signal_for(bars, len(bars) - 1))


# ─────────────────────────────────────────────────────────────────────────────
# Registry / factory wiring
# ─────────────────────────────────────────────────────────────────────────────

class RegistryTests(unittest.TestCase):

    def test_factory_builds_inversion_fvg(self) -> None:
        from gold_trader.research._factory_registry import make_strategy
        from gold_trader.research.experiments import default_inversion_fair_value_gap_grid
        params = default_inversion_fair_value_gap_grid()[0]
        strat = make_strategy("inversion_fair_value_gap", params)
        self.assertEqual(strat.name, "inversion_fair_value_gap")

    def test_factory_builds_rsi_divergence(self) -> None:
        from gold_trader.research._factory_registry import make_strategy
        from gold_trader.research.experiments import default_rsi_divergence_grid
        params = default_rsi_divergence_grid()[0]
        strat = make_strategy("rsi_divergence", params)
        self.assertEqual(strat.name, "rsi_divergence")

    def test_grids_non_empty(self) -> None:
        from gold_trader.research.experiments import (
            default_inversion_fair_value_gap_grid, default_rsi_divergence_grid,
        )
        self.assertGreater(len(default_inversion_fair_value_gap_grid()), 10)
        self.assertGreater(len(default_rsi_divergence_grid()), 50)


if __name__ == "__main__":
    unittest.main()
