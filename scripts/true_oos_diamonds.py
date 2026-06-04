"""TRUE out-of-sample diamond test.

Protocol (no peeking):
  1. Tune each family on bars 2023-05 .. 2025-05 ONLY (2 years train)
  2. Lock the chosen parameters
  3. Test on bars 2025-05 .. 2026-05 ONLY (1 year holdout, never seen)
  4. Report PF, n, avgR for each family individually + as ensemble

This is the ONLY protocol that tells the truth about whether grid
tuning is finding real patterns or fitting noise.
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


def find_best(spec, bars, config, subsample=30, min_trades=15):
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
        if s.total_trades < min_trades:
            continue
        netr = s.average_r * (s.total_trades / years)
        if netr > best_netr:
            best_netr = netr
            best = (params, s, netr)
    return best


def main():
    csv_path = "data/xauusd_5y/xauusd_5y_15m.csv"
    all_bars = load_bars_from_csv(csv_path)
    train_lo = datetime(2023, 5, 4, tzinfo=timezone.utc)
    train_hi = datetime(2025, 5, 4, tzinfo=timezone.utc)
    test_hi = datetime(2026, 5, 4, tzinfo=timezone.utc)
    train_bars = [b for b in all_bars if train_lo <= b.timestamp < train_hi]
    test_bars = [b for b in all_bars if train_hi <= b.timestamp <= test_hi]
    print(f"TRAIN: {len(train_bars)} bars  {train_bars[0].timestamp} .. {train_bars[-1].timestamp}")
    print(f"TEST:  {len(test_bars)} bars  {test_bars[0].timestamp} .. {test_bars[-1].timestamp}")

    config = BacktestConfig(slippage_bps=2.0, commission_per_trade=1.0)

    # ALL families considered, not pre-filtered.
    print("\n=== TUNING ON TRAIN (2023-05..2025-05) ===")
    print(f"{'family':30s} {'PF':>6} {'n':>4} {'avgR':>8} {'netR/yr':>9}")
    train_picks = {}
    for fam in all_self_contained_families():
        spec = family_spec(fam)
        best = find_best(spec, train_bars, config)
        if best is None:
            print(f"{fam:30s}   no valid config")
            continue
        params, s, netr = best
        train_picks[fam] = (params, s, netr)
        print(f"{fam:30s} {s.profit_factor:>6.2f} {s.total_trades:>4d} {s.average_r:>+8.3f} {netr:>+9.2f}")

    # Pick families with TRAIN netR/yr >= +0.5 — empirically meaningful in train
    diamonds = [(fam, params) for fam, (params, _, netr) in train_picks.items() if netr >= 0.5]
    print(f"\nTrain-positive (netR/yr >= +0.5): {len(diamonds)} families")
    for fam, _ in diamonds:
        print(f"  {fam}")

    # Apply locked params to TEST
    print("\n=== TEST (2025-05..2026-05) — TRUE OOS, locked params ===")
    print(f"{'family':30s} {'PF':>6} {'n':>4} {'avgR':>8} {'netR/yr':>9}")
    locked_strategies = []
    for fam, params in diamonds:
        spec = family_spec(fam)
        strat = spec.factory(params)
        locked_strategies.append(strat)
        bt = run_backtest(test_bars, strat, config)
        s = summarize_backtest(bt)
        years = (test_bars[-1].timestamp - test_bars[0].timestamp).days / 365.25
        netr = s.average_r * s.total_trades / years
        pf_disp = f"{s.profit_factor:>6.2f}" if s.profit_factor != float("inf") else "   inf"
        print(f"{fam:30s} {pf_disp} {s.total_trades:>4d} {s.average_r:>+8.3f} {netr:>+9.2f}")

    # Now the ensemble of locked diamonds on test
    print("\n=== TEST ENSEMBLE (locked diamonds, gate sweep) ===")
    print(f"{'mode':>15} {'gated':>6} {'n':>4} {'win%':>6} {'PF':>6} {'avgR':>7} {'finalEq':>8}")
    for gm in (1, 2, 3, 4):
        if gm > len(locked_strategies):
            continue
        res = run_ensemble_backtest(test_bars, locked_strategies, config, gate_min=gm)
        s = summarize_backtest(res.backtest)
        pf_disp = f"{s.profit_factor:>6.2f}" if s.profit_factor != float("inf") else "   inf"
        print(f"  ensemble-gm={gm}  {res.n_signals_gated_in:>6d} {s.total_trades:>4d} {s.win_rate:>5.1%} {pf_disp} {s.average_r:>+7.3f} {res.backtest.ending_equity:>8.0f}")


if __name__ == "__main__":
    main()
