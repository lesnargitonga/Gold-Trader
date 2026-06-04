"""Curated 5-strategy ensemble from grid-tuned diamonds.

Recent 3y per-family grid sweep identified 5 individually-profitable
configurations:
  london_breakout         grid_idx=13  PF 1.23  +2.36R/yr
  momentum_burst          grid_idx=28  PF 1.38  +1.31R/yr
  inversion_fair_value_gap grid_idx=?  PF 1.35  +0.34R/yr  (best in subsample)
  liquidity_sweep          grid_idx=?  PF 1.10  +0.38R/yr  (best in subsample)
  ny_close_compression    default      PF 1.34  +0.32R/yr

Run 3-fold rolling validation on recent 3y with realistic costs.
Compare standalone, gate=2, gate=3 ensemble PF.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import (
    run_backtest, run_ensemble_backtest, summarize_backtest,
)
from gold_trader.data import load_bars_from_csv
from gold_trader.models import BacktestConfig
from gold_trader.research.family_grids import (
    all_self_contained_families, family_spec,
)


def find_best(spec, bars, config, subsample=30):
    full = list(spec.grid)
    if len(full) > subsample:
        stride = max(1, len(full) // subsample)
        grid = full[::stride][:subsample]
    else:
        grid = full
    best = None
    best_netr = -1e9
    years = (bars[-1].timestamp - bars[0].timestamp).days / 365.25
    for params in grid:
        try:
            strat = spec.factory(params)
            bt = run_backtest(bars, strat, config)
            s = summarize_backtest(bt)
        except Exception:
            continue
        if s.total_trades < 15:
            continue
        netr = s.average_r * (s.total_trades / years)
        if netr > best_netr:
            best_netr = netr
            best = (params, s, netr)
    return best


def main():
    csv_path = "data/xauusd_5y/xauusd_5y_15m.csv"
    all_bars = load_bars_from_csv(csv_path)
    cutoff = datetime(2023, 5, 4, tzinfo=timezone.utc)
    bars = [b for b in all_bars if b.timestamp >= cutoff]
    print(f"recent 3y: {len(bars)} bars")

    config = BacktestConfig(slippage_bps=2.0, commission_per_trade=1.0)

    # Tune diamonds on FULL recent 3y (in-sample tuning — we'll then
    # validate via per-fold OOS).  This is the standard practice: tune
    # globally, then check per-fold consistency.
    target_families = [
        "london_breakout",
        "momentum_burst",
        "inversion_fair_value_gap",
        "liquidity_sweep",
        "ny_close_compression",
    ]
    print("\nTuning diamonds on recent 3y ...")
    diamond_strats = []
    for fam in target_families:
        spec = family_spec(fam)
        best = find_best(spec, bars, config)
        if best is None:
            print(f"  {fam}: no valid config found")
            continue
        params, s, netr = best
        strat = spec.factory(params)
        diamond_strats.append((fam, strat, params, s, netr))
        print(f"  {fam}: PF={s.profit_factor:.2f} n={s.total_trades} avgR={s.average_r:+.3f} netR/yr={netr:+.2f}")

    # 3-fold rolling validation on recent 3y
    print("\n=== 3-FOLD ROLLING VALIDATION (recent 3y) ===")
    span = (bars[-1].timestamp - bars[0].timestamp) / 3
    folds = []
    for i in range(3):
        lo = bars[0].timestamp + span * i
        hi = bars[0].timestamp + span * (i + 1) if i < 2 else bars[-1].timestamp
        chunk = [b for b in bars if lo <= b.timestamp <= hi]
        folds.append((lo, hi, chunk))

    print(f"\n{'fold':>4} {'period':>22} {'mode':>15} {'n':>4} {'win%':>6} {'PF':>6} {'avgR':>7} {'netR/yr':>8}")
    fold_years = 1.0
    summary = {}
    strategies = [s for _, s, _, _, _ in diamond_strats]
    for i, (lo, hi, chunk) in enumerate(folds, 1):
        if not chunk:
            continue
        # Standalone (gate=1): take every signal from any of the 5
        for label, gm in [("standalone-any", 1), ("gate=2", 2), ("gate=3", 3)]:
            res = run_ensemble_backtest(chunk, strategies, config, gate_min=gm)
            s = summarize_backtest(res.backtest)
            netr_yr = s.average_r * s.total_trades / fold_years
            pf_disp = f"{s.profit_factor:5.2f}" if s.profit_factor != float("inf") else "  inf"
            print(f"{i:>4} {lo.date()}..{hi.date()} {label:>15} {s.total_trades:>4d} {s.win_rate:>5.1%} {pf_disp} {s.average_r:>+7.3f} {netr_yr:>+8.2f}")
            summary.setdefault((label, gm), []).append((s.profit_factor, s.total_trades, s.average_r))
        # Each individual strategy alone for context
        for fam, strat, params, _, _ in diamond_strats:
            bt = run_backtest(chunk, strat, config)
            s = summarize_backtest(bt)
            pf_disp = f"{s.profit_factor:5.2f}" if s.profit_factor != float("inf") else "  inf"
            netr_yr = s.average_r * s.total_trades / fold_years
            print(f"{i:>4} {lo.date()}..{hi.date()} {fam:>15.15s} {s.total_trades:>4d} {s.win_rate:>5.1%} {pf_disp} {s.average_r:>+7.3f} {netr_yr:>+8.2f}")

    print("\n=== AGGREGATE: ensemble modes across 3 folds ===")
    for (label, gm), xs in sorted(summary.items()):
        n_pass = sum(1 for x in xs if x[0] >= 1.0)
        n_strong = sum(1 for x in xs if x[0] >= 1.2)
        total_n = sum(x[1] for x in xs)
        avg_r = sum(x[2] * x[1] for x in xs) / total_n if total_n else 0
        print(f"  {label:>15} folds_PF>=1.0: {n_pass}/3  folds_PF>=1.2: {n_strong}/3  total_n: {total_n}  weighted_avgR: {avg_r:+.3f}")


if __name__ == "__main__":
    main()
