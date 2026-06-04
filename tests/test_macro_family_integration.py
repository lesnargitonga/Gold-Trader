"""Integration tests for the Phase-14b macro family wiring.

These cover the end-to-end glue paths that take the validated
`timed_horizon_macro_regime` construct from family-grid registration all
the way through to agent-cycle's `build_bundle_snapshot`:

* `family_grids.family_spec_with_macro` returns a usable FamilySpec
* `make_strategy("timed_horizon_macro_regime", params, macro=...)` builds
  a runnable strategy
* `build_bundle_snapshot(..., macro_frame=...)` accepts the macro frame
  without raising and produces a sane snapshot
* `build_bundle_snapshot(..., macro_frame=None)` still works for the
  graceful-degradation path

We deliberately avoid asserting the strategy fires on synthetic bars —
the regime gate needs real DXY/VIX/real10y series and synthetic data
will not satisfy it.  The test is structural, not behavioural.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from gold_trader.data.macro import MacroFrame, MacroPoint, MacroSeries
from gold_trader.data.dukascopy import resample_bars
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.research import build_bundle_snapshot
from gold_trader.research.experiments import (
    TimedHorizonMacroRegimeParameters,
    default_timed_horizon_macro_regime_grid,
)
from gold_trader.research.family_grids import (
    EXTERNAL_DATA_FAMILIES,
    MACRO_FAMILIES,
    all_macro_families,
    family_spec_with_macro,
)
from gold_trader.research._factory_registry import make_strategy
from gold_trader.strategies.timed_horizon_macro_regime import (
    TimedHorizonMacroRegimeStrategy,
)


def _build_dummy_macro_frame() -> MacroFrame:
    """Minimum macro frame to satisfy the strategy's __init__ checks.

    Real coverage is exercised by integration jobs, not unit tests.
    """
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    points = [MacroPoint(timestamp=base + timedelta(days=i), value=float(i))
              for i in range(40)]
    series = {
        "real10y": MacroSeries(name="real10y", source="test", points=points),
        "dxy":     MacroSeries(name="dxy",     source="test", points=points),
        "vix":     MacroSeries(name="vix",     source="test", points=points),
    }
    return MacroFrame(series=series)


class MacroFamilyRegistrationTests(unittest.TestCase):
    def test_timed_horizon_listed_as_external_macro_family(self) -> None:
        self.assertIn("timed_horizon_macro_regime", EXTERNAL_DATA_FAMILIES)
        self.assertIn("timed_horizon_macro_regime", MACRO_FAMILIES)
        self.assertIn("timed_horizon_macro_regime", all_macro_families())

    def test_default_grid_contains_validated_cell(self) -> None:
        grid = default_timed_horizon_macro_regime_grid()
        self.assertGreater(len(grid), 0)
        # Validated PREMIUM cell from the Phase-14b audit
        winners = [
            p for p in grid
            if p.real_yield_lookback_days == 10
            and p.real_yield_max_change_bps == 0.0
            and p.vix_max_change_abs == 2.5
            and p.dxy_max_abs_change_pct == 1.0
            and p.far_atr_mult == 8.0
        ]
        self.assertEqual(len(winners), 1, "validated cell missing from default grid")

    def test_factory_builds_strategy_with_macro(self) -> None:
        macro = _build_dummy_macro_frame()
        params = TimedHorizonMacroRegimeParameters(
            real_yield_lookback_days=10,
            real_yield_max_change_bps=0.0,
            vix_max_change_abs=2.5,
            dxy_max_abs_change_pct=1.0,
            far_atr_mult=8.0,
        )
        strat = make_strategy("timed_horizon_macro_regime", params, macro=macro)
        self.assertIsInstance(strat, TimedHorizonMacroRegimeStrategy)
        self.assertEqual(strat.real_yield_lookback_days, 10)
        self.assertEqual(strat.far_atr_mult, 8.0)

    def test_factory_without_macro_raises(self) -> None:
        params = TimedHorizonMacroRegimeParameters(
            real_yield_lookback_days=10,
            real_yield_max_change_bps=0.0,
            vix_max_change_abs=2.5,
            dxy_max_abs_change_pct=1.0,
            far_atr_mult=8.0,
        )
        with self.assertRaises(ValueError):
            make_strategy("timed_horizon_macro_regime", params)

    def test_family_spec_with_macro_returns_usable_factory(self) -> None:
        macro = _build_dummy_macro_frame()
        spec = family_spec_with_macro("timed_horizon_macro_regime", macro)
        self.assertEqual(spec.name, "timed_horizon_macro_regime")
        self.assertGreater(len(spec.grid), 0)
        strat = spec.factory(spec.grid[0])
        self.assertIsInstance(strat, TimedHorizonMacroRegimeStrategy)


class MacroFamilyBundleIntegrationTests(unittest.TestCase):
    def test_snapshot_accepts_macro_kwarg(self) -> None:
        bars_5 = generate_synthetic_bars(count=600, seed=11)
        bars_60 = resample_bars(bars_5, interval_minutes=60)
        bars_240 = resample_bars(bars_5, interval_minutes=240)
        macro = _build_dummy_macro_frame()

        snapshot = build_bundle_snapshot(
            datasets={60: bars_60, 240: bars_240},
            families=["timed_horizon_macro_regime"],
            max_candidates=4,
            macro_frame=macro,
        )

        self.assertEqual(len(snapshot.timeframe_states), 2)
        # Either a candidate or hold; never an exception.
        self.assertIn(snapshot.decision.status, {"accept", "hold", "reject"})

    def test_snapshot_macro_family_skipped_when_frame_missing(self) -> None:
        bars_5 = generate_synthetic_bars(count=400, seed=17)
        bars_60 = resample_bars(bars_5, interval_minutes=60)
        bars_240 = resample_bars(bars_5, interval_minutes=240)

        snapshot = build_bundle_snapshot(
            datasets={60: bars_60, 240: bars_240},
            families=["timed_horizon_macro_regime"],
            max_candidates=4,
            macro_frame=None,
        )

        # No candidates should come from the macro family — graceful degrade.
        macro_candidates = [
            c for c in snapshot.entry_candidates
            if c.family == "timed_horizon_macro_regime"
        ]
        self.assertEqual(macro_candidates, [])


if __name__ == "__main__":
    unittest.main()
