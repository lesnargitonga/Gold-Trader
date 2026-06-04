"""Regression guard: pin the validated edge of the MTF system.

The Phase 11 clinical evaluation produced a single configuration with
statistically meaningful out-of-sample performance over 5 years of XAUUSD:

    HTFBreakoutContinuation(align_tf="240m", range_lookback=18, risk_reward=2.5)
    wrapped in
    RegimeGatedMTF(align_tf="240m", min_trend_strength_atr=0.5,
                   atr_pct_window=100, atr_pct_low=0.20, atr_pct_high=0.90)

Across the canonical 5-fold ladder Y1..Y5 (2021-05 -> 2026-05) at retail
costs ($1/trade + 2 bps slippage), this configuration produced exactly:

    n_trades = 244
    sum(pnl_r) = +13.4724  (avg_R = +0.0552)

    per-fold PF: Y1 0.90 / Y2 1.37 / Y3 1.06 / Y4 1.38 / Y5 1.30
    one-sided block-bootstrap P(avg_R <= 0) = 0.190 (NOT significant)

This test pins those numbers as a regression guard.  If a future change
silently drifts the result by more than the tolerance below, this test
fails and the change must be justified.

The pinned numbers correspond to the post-Phase-13 weekend-gap-guard
state: HTFBreakoutContinuation now rejects bars whose `range_lookback`
window crosses a multi-hour gap (weekend, holiday).  This dropped 31
trades (12% of sample) carrying ~47% of total R, moving the bootstrap
p from 0.047 -> 0.190.  The remaining edge is below the 5% threshold
and cannot justify forward live capital.

History of pinned magnitudes (drifts only down as bookkeeping bugs get
corrected):
    Phase 11 (R:R drift bug, gap-noise included):  n=277, +26.54R
    Phase 12 (R:R fixed, gap-noise included):      n=275, +25.31R
    Phase 13 (gap-guard added, this test):         n=244, +13.47R

The test is skipped automatically if the canonical 5y dataset isn't
present (CI / fresh checkouts).
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from gold_trader.backtest import build_indicator_caches, run_mtf_backtest
from gold_trader.data import build_mtf_bundle
from gold_trader.models import BacktestConfig
from gold_trader.strategies.mtf_strategies import (
    HTFBreakoutContinuation,
    RegimeGatedMTF,
)
from gold_trader.validation import load_5y_ladder, slice_window


REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_5Y_DIR = REPO_ROOT / "data" / "xauusd_5y"

EXPECTED_N_TRADES = 244
EXPECTED_TOTAL_R = 13.4724
TOTAL_R_TOLERANCE = 0.05

CONFIG = BacktestConfig(
    starting_equity=100_000.0,
    risk_fraction=0.01,
    max_hold_bars=24,
    kill_switch_drawdown_fraction=None,
    slippage_bps=2.0,
    commission_per_trade=1.0,
)

SPLITS = [
    (datetime(2021, 5, 4, tzinfo=timezone.utc), datetime(2022, 5, 4, tzinfo=timezone.utc)),
    (datetime(2022, 5, 4, tzinfo=timezone.utc), datetime(2023, 5, 4, tzinfo=timezone.utc)),
    (datetime(2023, 5, 4, tzinfo=timezone.utc), datetime(2024, 5, 4, tzinfo=timezone.utc)),
    (datetime(2024, 5, 4, tzinfo=timezone.utc), datetime(2025, 5, 4, tzinfo=timezone.utc)),
    (datetime(2025, 5, 4, tzinfo=timezone.utc), datetime(2026, 5, 4, tzinfo=timezone.utc)),
]


class MTFEdgeRegressionTest(unittest.TestCase):
    @unittest.skipUnless(
        CANONICAL_5Y_DIR.exists(),
        f"canonical 5y dataset not present at {CANONICAL_5Y_DIR}",
    )
    def test_5y_aggregate_pinned(self):
        primary, htf_bundle = load_5y_ladder(
            primary_tf="60m",
            htf_tfs=["240m", "1440m"],
            time_lo=datetime(2021, 4, 1, tzinfo=timezone.utc),
            time_hi=datetime(2026, 5, 4, tzinfo=timezone.utc),
        )
        inner = HTFBreakoutContinuation(
            align_tf="240m", range_lookback=18, risk_reward=2.5,
        )
        gated = RegimeGatedMTF(
            inner=inner, align_tf="240m",
            min_trend_strength_atr=0.5,
            atr_pct_window=100,
            atr_pct_low=0.20, atr_pct_high=0.90,
        )

        total_trades = 0
        total_r = 0.0
        for lo, hi in SPLITS:
            p_slice, h_slice = slice_window(primary, htf_bundle, lo, hi)
            bundle = build_mtf_bundle("60m", p_slice, h_slice)
            indicators = build_indicator_caches(bundle)
            res = run_mtf_backtest(bundle, gated, CONFIG, indicators=indicators)
            total_trades += len(res.trades)
            total_r += sum(t.pnl_r for t in res.trades)

        self.assertEqual(
            total_trades, EXPECTED_N_TRADES,
            f"trade count drifted: got {total_trades} expected {EXPECTED_N_TRADES}",
        )
        self.assertAlmostEqual(
            total_r, EXPECTED_TOTAL_R, delta=TOTAL_R_TOLERANCE,
            msg=f"5y aggregate pnl_r drifted: got {total_r:+.4f} "
                f"expected {EXPECTED_TOTAL_R:+.4f} (tol {TOTAL_R_TOLERANCE})",
        )


if __name__ == "__main__":
    unittest.main()
