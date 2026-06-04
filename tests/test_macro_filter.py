"""Tests for MacroDecisionFilter and TimedHorizonMacroRegimeStrategy."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from gold_trader.data.macro import MacroFrame, MacroPoint, MacroSeries
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.macro_filter import MacroDecisionFilter
from gold_trader.models import Side
from gold_trader.strategies.timed_horizon_macro_regime import (
    TimedHorizonMacroRegimeStrategy,
)


def _series(name: str, values: list[tuple[str, float]]) -> MacroSeries:
    pts = [
        MacroPoint(
            timestamp=datetime.fromisoformat(d).replace(tzinfo=timezone.utc),
            value=v,
        )
        for d, v in values
    ]
    return MacroSeries(name=name, source="test", points=pts)


def _build_macro_frame_bullish() -> MacroFrame:
    """real10y falling, vix calm, dxy flat — the textbook bullish gold regime."""
    real_pts = [
        ("2026-04-01", 2.10),
        ("2026-04-15", 2.05),
        ("2026-04-22", 1.95),
        ("2026-04-29", 1.85),
        ("2026-05-04", 1.75),
    ]
    vix_pts = [
        ("2026-04-01", 14.0),
        ("2026-04-15", 14.5),
        ("2026-04-22", 14.2),
        ("2026-04-29", 14.6),
        ("2026-05-04", 14.4),
    ]
    dxy_pts = [
        ("2026-04-01", 100.0),
        ("2026-04-15", 100.2),
        ("2026-04-22", 99.9),
        ("2026-04-29", 100.1),
        ("2026-05-04", 100.0),
    ]
    bei_pts = [
        ("2026-04-01", 2.30),
        ("2026-05-04", 2.35),
    ]
    # Pad with enough points so tertile cuts exist (>=9 each).
    extra_real = [(f"2025-{m:02d}-01", 2.0 + i * 0.05) for i, m in enumerate(range(1, 13))]
    # bei10: keep <9 points so stagflation tertile check is skipped (we want long allowed).
    return MacroFrame(series={
        "real10y": _series("real10y", extra_real + real_pts),
        "vix": _series("vix", vix_pts),
        "dxy": _series("dxy", dxy_pts),
        "bei10": _series("bei10", bei_pts),
    })


def _build_macro_frame_bearish() -> MacroFrame:
    """real10y rising sharply — short tailwind, long headwind."""
    real_pts = [
        ("2026-04-01", 1.50),
        ("2026-04-15", 1.60),
        ("2026-04-22", 1.75),
        ("2026-04-29", 1.95),
        ("2026-05-04", 2.10),  # +60bps over 5d ending 2026-05-04
    ]
    vix_pts = [("2026-04-01", 16.0), ("2026-05-04", 16.2)]
    dxy_pts = [
        ("2026-04-01", 100.0),
        ("2026-05-04", 102.5),  # +2.5% over period
    ]
    bei_pts = [("2026-04-01", 2.30), ("2026-05-04", 2.40)]
    extra_real = [(f"2025-{m:02d}-01", 1.4 + i * 0.05) for i, m in enumerate(range(1, 13))]
    return MacroFrame(series={
        "real10y": _series("real10y", extra_real + real_pts),
        "vix": _series("vix", vix_pts),
        "dxy": _series("dxy", dxy_pts),
        "bei10": _series("bei10", bei_pts),
    })


class MacroDecisionFilterTests(unittest.TestCase):
    def test_bullish_regime_allows_long(self) -> None:
        f = MacroDecisionFilter(macro=_build_macro_frame_bullish())
        ts = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        d = f.evaluate(Side.LONG, ts)
        self.assertEqual(d.verdict, "allow")
        self.assertIn("real10y_falling", d.regime_tags)

    def test_bearish_regime_blocks_long(self) -> None:
        f = MacroDecisionFilter(macro=_build_macro_frame_bearish())
        ts = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        d = f.evaluate(Side.LONG, ts)
        self.assertEqual(d.verdict, "block")
        # Should be blocked on either real_yield_rising or dxy_strong rule.
        self.assertTrue(
            "real10y" in d.reason or "DXY" in d.reason,
            f"unexpected reason: {d.reason}",
        )

    def test_bearish_regime_allows_short(self) -> None:
        f = MacroDecisionFilter(macro=_build_macro_frame_bearish())
        ts = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        d = f.evaluate(Side.SHORT, ts)
        self.assertEqual(d.verdict, "allow")

    def test_bullish_regime_blocks_short(self) -> None:
        f = MacroDecisionFilter(macro=_build_macro_frame_bullish())
        ts = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        d = f.evaluate(Side.SHORT, ts)
        self.assertEqual(d.verdict, "block")

    def test_missing_data_returns_warning_not_crash(self) -> None:
        # Empty macro frame - no series at all.
        f = MacroDecisionFilter(macro=MacroFrame(series={}))
        ts = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        d = f.evaluate(Side.LONG, ts)
        # No data = no support either way; with warn_unsupportive default
        # this returns allow_with_warning.
        self.assertIn(d.verdict, ("allow", "allow_with_warning"))


class TimedHorizonMacroRegimeTests(unittest.TestCase):
    def test_warmup_and_no_macro_returns_none(self) -> None:
        bars = generate_synthetic_bars(count=80, seed=11)
        s = TimedHorizonMacroRegimeStrategy(macro=MacroFrame(series={}))
        for i in range(s.warmup_bars(), len(bars)):
            self.assertIsNone(s.signal_for(bars, i))

    def test_signal_geometry_when_regime_ok(self) -> None:
        bars = generate_synthetic_bars(count=200, seed=17)
        macro = _build_macro_frame_bullish()
        # Push synthetic bars into the regime's date window.
        from dataclasses import replace as _replace
        # synthetic bars are tz-naive; stay naive for the offset, then attach tz.
        target_start = datetime(2026, 4, 28, 0, 0)
        offset = target_start - bars[0].timestamp
        bars = [
            _replace(b, timestamp=(b.timestamp + offset).replace(tzinfo=timezone.utc))
            for b in bars
        ]
        s = TimedHorizonMacroRegimeStrategy(
            macro=macro,
            real_yield_max_change_bps=0.0,
            vix_max_change_abs=10.0,
            dxy_max_abs_change_pct=10.0,
            require_dxy_flat=True,
            allowed_sessions=("asia", "london", "new_york"),
            once_per_day=False,
        )
        found = None
        for i in range(s.warmup_bars(), len(bars)):
            sig = s.signal_for(bars, i)
            if sig is not None:
                found = (i, sig)
                break
        self.assertIsNotNone(found, "expected at least one signal under bullish regime")
        idx, sig = found
        self.assertEqual(sig.side, Side.LONG)
        # Stop should be far below entry (10 ATR by default).
        self.assertLess(sig.stop, bars[idx].close)
        # Target above entry.
        self.assertGreater(sig.target, bars[idx].close)
        # risk_reward must be 0 so engine doesn't recompute (we want time-exit).
        self.assertEqual(sig.risk_reward, 0.0)


if __name__ == "__main__":
    unittest.main()
