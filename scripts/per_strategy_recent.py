"""Per-strategy honest baseline on recent 3y — costs included.

The concurrence gate produces ~27 trades/year because it requires 6 of
15 strategies to agree on the SAME 15m bar.  This is statistically
restrictive and may be hiding individual strategies that work alone.

This script:
  1. Loads recent 3y of 15m bars
  2. Runs EACH strategy alone with realistic costs
  3. Reports per-strategy: trades, win%, PF, avgR, expectancy/yr
  4. Sorts by net annual expectancy
  5. Then tries the top-3 strategies as a SMALL ensemble (gate=2)
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


def main():
    csv_path = "data/xauusd_5y/xauusd_5y_15m.csv"
    print(f"loading {csv_path} ...")
    all_bars = load_bars_from_csv(csv_path)
    cutoff = datetime(2023, 5, 4, tzinfo=timezone.utc)
    bars = [b for b in all_bars if b.timestamp >= cutoff]
    years = (bars[-1].timestamp - bars[0].timestamp).days / 365.25
    print(f"  recent {years:.1f}y: {len(bars)} bars")

    config = BacktestConfig(slippage_bps=2.0, commission_per_trade=1.0)

    rows = []
    print("\n=== PER-STRATEGY (default params, recent 3y, costs included) ===")
    print(f"{'strategy':30s} {'n':>5} {'win%':>7} {'PF':>6} {'avgR':>8} {'n/yr':>6} {'netR/yr':>8}")
    print("-" * 78)
    strategies = []
    for fam in all_self_contained_families():
        spec = family_spec(fam)
        strat = spec.factory(spec.grid[0])
        strategies.append(strat)
        bt = run_backtest(bars, strat, config)
        s = summarize_backtest(bt)
        n_yr = s.total_trades / years
        net_r_yr = s.average_r * n_yr
        rows.append((fam, s.total_trades, s.win_rate, s.profit_factor, s.average_r, n_yr, net_r_yr))
        pf = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "inf"
        print(f"{fam:30s} {s.total_trades:>5d} {s.win_rate:>6.1%} {pf:>6s} "
              f"{s.average_r:>+8.3f} {n_yr:>6.1f} {net_r_yr:>+8.2f}")

    # Sort by net R/yr
    rows.sort(key=lambda r: r[6], reverse=True)
    print("\n=== RANKED BY NET R/YEAR ===")
    print(f"{'rank':>4} {'strategy':30s} {'PF':>6} {'n/yr':>6} {'netR/yr':>8}")
    for i, r in enumerate(rows, start=1):
        pf = f"{r[3]:.2f}" if r[3] != float("inf") else "inf"
        print(f"{i:>4} {r[0]:30s} {pf:>6s} {r[5]:>6.1f} {r[6]:>+8.2f}")

    # Pick top-K by net-R/yr and try a small high-quality ensemble
    print("\n=== SMALL ENSEMBLE (top-K by netR/yr, gate sweep) ===")
    name_to_strat = {fam: s for fam, s in zip([f for f in all_self_contained_families()], strategies)}
    for K in (3, 5, 7):
        top = [name_to_strat[r[0]] for r in rows[:K] if r[3] >= 1.0 or r[3] == float("inf")]
        if len(top) < 2:
            continue
        print(f"\n  top {len(top)} (PF>=1 only): {[t.name for t in top]}")
        for gm in (1, 2, 3):
            if gm > len(top):
                continue
            res = run_ensemble_backtest(bars, top, config, gate_min=gm)
            s = summarize_backtest(res.backtest)
            n_yr = s.total_trades / years
            pf = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "inf"
            print(f"    gate={gm}: n={s.total_trades:3d} ({n_yr:.1f}/yr)  "
                  f"win={s.win_rate:.1%}  PF={pf}  avgR={s.average_r:+.3f}  "
                  f"netR/yr={s.average_r * n_yr:+.2f}  eq={res.backtest.ending_equity:.0f}")


if __name__ == "__main__":
    main()
