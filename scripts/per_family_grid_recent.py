"""Per-family GRID sweep on recent 3y — find if profitable params exist
inside each strategy family.  Default-params test showed 14/15 net
negative.  This asks the right question: are the FAMILIES bad, or just
the default configurations?
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import run_backtest, summarize_backtest
from gold_trader.data import load_bars_from_csv
from gold_trader.models import BacktestConfig
from gold_trader.research.family_grids import (
    all_self_contained_families, family_spec,
)


def main():
    csv_path = "data/xauusd_5y/xauusd_5y_15m.csv"
    all_bars = load_bars_from_csv(csv_path)
    cutoff = datetime(2023, 5, 4, tzinfo=timezone.utc)
    bars = [b for b in all_bars if b.timestamp >= cutoff]
    years = (bars[-1].timestamp - bars[0].timestamp).days / 365.25
    print(f"recent {years:.1f}y: {len(bars)} bars\n")

    config = BacktestConfig(slippage_bps=2.0, commission_per_trade=1.0)

    print(f"{'family':30s} {'grid':>5} {'best_PF':>8} {'best_n/y':>9} {'best_netR/y':>12} {'def_PF':>7} {'def_netR/y':>11}")
    print("-" * 90)
    diamonds = []
    SUBSAMPLE = 30
    for fam in all_self_contained_families():
        spec = family_spec(fam)
        full_grid = list(spec.grid)
        if len(full_grid) > SUBSAMPLE:
            stride = max(1, len(full_grid) // SUBSAMPLE)
            grid = full_grid[::stride][:SUBSAMPLE]
        else:
            grid = full_grid
        # Default = grid[0]
        def_strat = spec.factory(grid[0])
        def_bt = run_backtest(bars, def_strat, config)
        def_s = summarize_backtest(def_bt)
        def_pf = def_s.profit_factor
        def_netr = def_s.average_r * (def_s.total_trades / years)

        best_pf = 0.0
        best_n = 0
        best_avgr = 0.0
        best_idx = -1
        for i, params in enumerate(grid):
            try:
                strat = spec.factory(params)
                bt = run_backtest(bars, strat, config)
                s = summarize_backtest(bt)
            except Exception:
                continue
            if s.total_trades < 15:
                continue  # need minimum sample to trust
            pf = s.profit_factor if s.profit_factor != float("inf") else 99.0
            netr = s.average_r * (s.total_trades / years)
            score = netr  # rank by net R/yr
            if score > best_avgr * (best_n / years if best_n else 1):
                best_pf = s.profit_factor
                best_n = s.total_trades
                best_avgr = s.average_r
                best_idx = i

        best_n_yr = best_n / years if best_n else 0
        best_netr = best_avgr * best_n_yr
        if best_idx >= 0 and best_netr > 0.5:
            diamonds.append((fam, best_idx, grid[best_idx], best_pf, best_n_yr, best_netr))
        bp = f"{best_pf:.2f}" if best_pf < 99 else "inf"
        dp = f"{def_pf:.2f}" if def_pf != float("inf") else "inf"
        print(f"{fam:30s} {len(grid):>5d} {bp:>8s} {best_n_yr:>9.1f} {best_netr:>+12.2f} {dp:>7s} {def_netr:>+11.2f}")

    print(f"\n=== DIAMONDS (best params, netR/yr > +0.5) — {len(diamonds)} of {len(list(all_self_contained_families()))} families ===")
    for fam, idx, params, pf, n_yr, netr in sorted(diamonds, key=lambda x: -x[5]):
        print(f"  {fam:30s} grid_idx={idx:3d} PF={pf:.2f} n/yr={n_yr:.1f} netR/yr={netr:+.2f}")
        print(f"    params={params}")


if __name__ == "__main__":
    main()
