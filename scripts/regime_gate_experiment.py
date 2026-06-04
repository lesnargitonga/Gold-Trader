"""Layer a macro-regime filter on top of the concurrence gate and
quantify the lift versus concurrence-only.

Hypothesis (from the May 8 macro sweep): gold is a macro-driven asset
and intraday entries should only fire when the macro regime is
favourable.  The strongest empirical signals were:

    real10y trending DOWN  (5d change)   -> bullish gold
    vix calm/elevated      (NOT stressed)
    dxy weak or flat       (20d change)
    NOT macro_stagflation  (bei10 hi & real10y lo)

This script runs the concurrence-gated ensemble with and without a
regime overlay on the holdout test slice and prints the result side-by-
side.  Costs are realistic (slippage 2bps + $1 commission) so the
numbers are tradeable, not gross.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import run_ensemble_backtest, summarize_backtest
from gold_trader.data import load_bars_from_csv, load_macro_frame
from gold_trader.models import BacktestConfig, Side
from gold_trader.regime import RegimeDetector
from gold_trader.research.family_grids import (
    all_self_contained_families, family_spec,
)


def build_strategies():
    out = []
    for fam in all_self_contained_families():
        spec = family_spec(fam)
        out.append(spec.factory(spec.grid[0]))
    return out


def make_regime_filter(bars, macro):
    detector = RegimeDetector()
    cache: dict[int, bool] = {}

    def _favorable(_bars, idx, side):
        # cache per (idx, side) — same idx may be probed twice if both
        # sides hit the gate (rare, but cheap insurance).
        key = idx * 2 + (0 if side is Side.LONG else 1)
        if key in cache:
            return cache[key]
        try:
            tags = detector.classify(bars, idx, macro=macro)
        except Exception:
            cache[key] = False
            return False

        # Long-only filter (gold = inverse of dollar/real-rate strength).
        # Short side: invert the conditions.
        if side is Side.LONG:
            ok = (
                tags.macro_real10y in {"falling", "flat"}
                and tags.macro_vix in {"calm", "elevated"}
                and tags.macro_dxy in {"weak", "flat"}
                and not tags.macro_stagflation
            )
        else:
            ok = (
                tags.macro_real10y in {"rising", "flat"}
                and tags.macro_vix in {"calm", "elevated"}
                and tags.macro_dxy in {"strong", "flat"}
            )
        cache[key] = ok
        return ok

    return _favorable


def run_one(label, bars, strategies, config, gate_min, *, bar_filter=None):
    res = run_ensemble_backtest(
        bars, strategies, config,
        gate_min=gate_min, bar_filter=bar_filter,
    )
    s = summarize_backtest(res.backtest)
    print(
        f"  {label:30s}  gated={res.n_signals_gated_in:5d}  "
        f"trades={s.total_trades:4d}  PF={s.profit_factor:5.2f}  "
        f"win={s.win_rate:5.1%}  avgR={s.average_r:+.3f}  "
        f"eq={res.backtest.ending_equity:.0f}"
    )
    return s, res


def main():
    csv_train = "data/xauusd_5y_walkforward/train_4y_15m.csv"
    csv_test = "data/xauusd_5y_walkforward/test_1y_15m.csv"
    macro_dir = "data/macro"

    config = BacktestConfig(
        slippage_bps=2.0,
        commission_per_trade=1.0,
    )

    macro = load_macro_frame(macro_dir)
    print(f"macro series loaded: {macro.names()}")

    strategies = build_strategies()
    print(f"strategies: {len(strategies)}")

    for label, csv_path in [("TRAIN", csv_train), ("TEST", csv_test)]:
        print(f"\n=== {label} ({csv_path}) ===")
        bars = load_bars_from_csv(csv_path)
        if not bars:
            print(f"  (no bars at {csv_path})")
            continue
        regime_filter = make_regime_filter(bars, macro)
        for gm in (5, 6, 7):
            print(f" gate_min={gm}")
            run_one(f"plain (concurrence-only)", bars, strategies, config, gm)
            run_one(f"regime-gated (macro overlay)", bars, strategies, config, gm,
                    bar_filter=regime_filter)


if __name__ == "__main__":
    main()
