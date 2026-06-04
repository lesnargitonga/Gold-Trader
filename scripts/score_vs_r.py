"""Score-vs-R calibration analysis.

Runs a scored strategy on a CSV, then groups closed trades by their
per-signal score (carried via TradeSignal.score → ExecutedTrade.score)
and reports per-bucket PF / avg_R / win_rate.

This is the empirical calibration data that should inform the
70/55/40 verdict thresholds in strategies/scoring.py.

Usage::

    PYTHONPATH=src .venv/bin/python scripts/score_vs_r.py \\
        data/xauusd_5y/xauusd_5y_15m.csv \\
        --family inversion_fair_value_gap

Prints a table::

    score_bucket  n   wins  win_rate  avg_R   PF
    [40,50)       ..  ..    ..        ..      ..
    [50,55)       ..  ..    ..        ..      ..
    [55,60)       ..  ..    ..        ..      ..
    [60,70)       ..  ..    ..        ..      ..
    [70,80)       ..  ..    ..        ..      ..
    [80,90)       ..  ..    ..        ..      ..
    [90,100]      ..  ..    ..        ..      ..
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gold_trader.data import load_bars_from_csv
from gold_trader.backtest.engine import run_backtest
from gold_trader.models import BacktestConfig
from gold_trader.research.family_grids import family_spec


def _bucket(score: float) -> str:
    if score < 40: return "<40"
    if score < 50: return "[40,50)"
    if score < 55: return "[50,55)"
    if score < 60: return "[55,60)"
    if score < 70: return "[60,70)"
    if score < 80: return "[70,80)"
    if score < 90: return "[80,90)"
    return "[90,100]"


_BUCKET_ORDER = ["<40", "[40,50)", "[50,55)", "[55,60)", "[60,70)",
                 "[70,80)", "[80,90)", "[90,100]"]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("csv_path")
    p.add_argument("--family", required=True)
    p.add_argument("--params-index", type=int, default=0)
    p.add_argument("--pool-grid", action="store_true",
                   help="Run every grid combo and aggregate (slow).")
    args = p.parse_args()

    bars = load_bars_from_csv(args.csv_path)
    print(f"loaded {len(bars)} bars from {args.csv_path}")
    spec = family_spec(args.family)
    grid = spec.grid if args.pool_grid else [spec.grid[args.params_index]]
    print(f"running {len(grid)} param combo(s)")

    cfg = BacktestConfig()
    # bucket -> [n, wins, sum_r, sum_pos_r, sum_neg_r]
    stats: dict[str, list[float]] = {}
    for params in grid:
        s = spec.factory(params)
        result = run_backtest(bars, s, cfg)
        for tr in result.trades:
            b = _bucket(tr.score)
            if b not in stats:
                stats[b] = [0, 0, 0.0, 0.0, 0.0]
            stats[b][0] += 1
            if tr.pnl_r > 0:
                stats[b][1] += 1
                stats[b][3] += tr.pnl_r
            else:
                stats[b][4] += tr.pnl_r
            stats[b][2] += tr.pnl_r

    print()
    print(f"{'bucket':<10s} {'n':>6s} {'wins':>6s} {'win%':>6s} {'avg_R':>8s} "
          f"{'PF':>6s} {'sum_R':>9s}")
    print("-" * 60)
    total_n = total_w = 0
    total_r = total_pos = total_neg = 0.0
    for b in _BUCKET_ORDER:
        if b not in stats:
            continue
        n, w, sumr, posr, negr = stats[b]
        wr = (w / n * 100) if n else 0.0
        avg_r = (sumr / n) if n else 0.0
        pf = (posr / abs(negr)) if negr < 0 else float('inf')
        pf_s = f"{pf:>6.2f}" if pf != float('inf') else "   inf"
        print(f"{b:<10s} {int(n):>6d} {int(w):>6d} {wr:>5.1f}% {avg_r:>+8.3f} "
              f"{pf_s} {sumr:>+9.2f}")
        total_n += n
        total_w += w
        total_r += sumr
        total_pos += posr
        total_neg += negr
    print("-" * 60)
    if total_n:
        wr = total_w / total_n * 100
        avg = total_r / total_n
        pf = total_pos / abs(total_neg) if total_neg < 0 else float('inf')
        pf_s = f"{pf:>6.2f}" if pf != float('inf') else "   inf"
        print(f"{'TOTAL':<10s} {int(total_n):>6d} {int(total_w):>6d} {wr:>5.1f}% "
              f"{avg:>+8.3f} {pf_s} {total_r:>+9.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
