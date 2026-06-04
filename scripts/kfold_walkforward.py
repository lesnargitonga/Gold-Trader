"""5-fold rolling walk-forward validation of the concurrence gate.

Splits 5y of 15m bars into 5 contiguous 1-year folds.  For each fold,
runs the gated ensemble with realistic costs and reports per-fold PF.
A gate threshold is "robust" only if it passes (PF >= threshold) on
the majority of out-of-sample folds.

This is THE go/no-go test for the concurrence-gated technical system.
If gate=6 produces PF >= 1.0 on >= 4 of 5 folds, we have a real
(modest) edge to deploy live with size discipline.  If not, the
intraday technical universe is exhausted on 15m gold and the project
must pivot category (different timeframe, asset, or
macro-conditioned-swing rather than 15m intraday).
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import run_ensemble_backtest, summarize_backtest
from gold_trader.data import load_bars_from_csv
from gold_trader.models import BacktestConfig
from gold_trader.research.family_grids import (
    all_self_contained_families, family_spec,
)


def build_strategies():
    out = []
    for fam in all_self_contained_families():
        spec = family_spec(fam)
        out.append(spec.factory(spec.grid[0]))
    return out


def split_into_folds(bars, n_folds=5):
    if not bars:
        return []
    t0 = bars[0].timestamp
    t1 = bars[-1].timestamp
    span = t1 - t0
    fold_span = span / n_folds
    edges = [t0 + fold_span * i for i in range(n_folds + 1)]
    edges[-1] = t1  # avoid float-tail loss
    folds = []
    for i in range(n_folds):
        lo, hi = edges[i], edges[i + 1]
        chunk = [b for b in bars if lo <= b.timestamp < hi or (i == n_folds - 1 and b.timestamp == hi)]
        folds.append((lo, hi, chunk))
    return folds


def main():
    csv_path = "data/xauusd_5y/xauusd_5y_15m.csv"
    print(f"loading {csv_path} ...")
    bars = load_bars_from_csv(csv_path)
    print(f"  {len(bars)} bars  {bars[0].timestamp} .. {bars[-1].timestamp}")

    config = BacktestConfig(slippage_bps=2.0, commission_per_trade=1.0)
    strategies = build_strategies()
    print(f"  {len(strategies)} strategies, costs: 2bps slippage + $1 commission\n")

    folds = split_into_folds(bars, n_folds=5)
    rows = []
    header = ["fold", "from", "to", "bars", "gate_min", "trades", "win%", "PF", "avgR", "final_eq"]
    print(" | ".join(f"{h:>10}" for h in header))
    print("-" * (12 * len(header)))

    for i, (lo, hi, chunk) in enumerate(folds, start=1):
        if len(chunk) < 1000:
            print(f"fold {i}: too small ({len(chunk)} bars), skipping")
            continue
        for gm in (5, 6, 7):
            res = run_ensemble_backtest(chunk, strategies, config, gate_min=gm)
            s = summarize_backtest(res.backtest)
            row = [
                str(i),
                lo.date().isoformat(),
                hi.date().isoformat(),
                str(len(chunk)),
                str(gm),
                str(s.total_trades),
                f"{s.win_rate:.1%}",
                f"{s.profit_factor:.2f}",
                f"{s.average_r:+.3f}",
                f"{res.backtest.ending_equity:.0f}",
            ]
            rows.append(row)
            print(" | ".join(f"{v:>10}" for v in row))

    # Summary by gate_min: count of folds with PF>=1.0
    print("\n=== ROBUSTNESS SUMMARY ===")
    for gm in (5, 6, 7):
        gm_rows = [r for r in rows if r[4] == str(gm)]
        if not gm_rows:
            continue
        pfs = []
        for r in gm_rows:
            try:
                pfs.append(float(r[7]) if r[7] != "inf" else float("inf"))
            except ValueError:
                pfs.append(0.0)
        n_pass = sum(1 for pf in pfs if pf >= 1.0)
        n_strong = sum(1 for pf in pfs if pf >= 1.2)
        avg_pf = sum(p for p in pfs if p != float("inf")) / max(1, sum(1 for p in pfs if p != float("inf")))
        total_trades = sum(int(r[5]) for r in gm_rows)
        print(
            f"gate={gm}  folds_PF>=1.0: {n_pass}/{len(gm_rows)}  "
            f"folds_PF>=1.2: {n_strong}/{len(gm_rows)}  "
            f"avgPF (finite): {avg_pf:.2f}  total_trades: {total_trades}"
        )

    out_path = Path("reports/observatory/kfold/results.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
