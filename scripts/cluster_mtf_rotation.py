"""Rotation validation of cluster + HTF confluence.

Three rolling train/test splits within recent 3y:
  Split A: train 2023-05..2024-11 (1.5y), test 2024-11..2025-05 (0.5y)
  Split B: train 2023-11..2025-05 (1.5y), test 2025-05..2025-11 (0.5y)
  Split C: train 2024-05..2025-11 (1.5y), test 2025-11..2026-05 (0.5y)

For each split:
  1. Tune best in each cluster on train (locked thereafter)
  2. Build 240m HTF trend lookup over test window
  3. Apply gate=2 + HTF-follow on test
  4. Report PF, n, avgR

PASS criterion: gate=2+HTF PF >= 1.0 on >= 2 of 3 splits.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import (
    run_backtest, run_ensemble_backtest, summarize_backtest,
)
from gold_trader.data import load_bars_from_csv
from gold_trader.models import BacktestConfig, Side
from gold_trader.research.family_grids import family_spec


CLUSTER_BREAKOUT = [
    "opening_range_breakout", "asian_range_breakout", "london_breakout",
    "previous_day_breakout", "ny_session_breakout", "compression_breakout",
    "momentum_burst", "session_continuation", "trend_pullback",
]
CLUSTER_REVERSION = ["asian_range_fade", "ny_close_compression", "rsi_divergence"]
CLUSTER_ICT = ["fair_value_gap", "inversion_fair_value_gap", "liquidity_sweep"]


def find_best_in_family(spec, bars, config, subsample=30, min_trades=15):
    full = list(spec.grid)
    if len(full) > subsample:
        stride = max(1, len(full) // subsample)
        grid = full[::stride][:subsample]
    else:
        grid = full
    best = None
    best_score = -1e9
    years = max(0.1, (bars[-1].timestamp - bars[0].timestamp).days / 365.25)
    for params in grid:
        try:
            strat = spec.factory(params)
            bt = run_backtest(bars, strat, config)
            s = summarize_backtest(bt)
        except Exception:
            continue
        if s.total_trades < min_trades:
            continue
        if s.profit_factor < 1.05:
            continue
        netr = s.average_r * (s.total_trades / years)
        if netr > best_score:
            best_score = netr
            best = (params, s, netr)
    return best


def champion_per_cluster(bars, config, fams):
    best_overall = None
    best_score = -1e9
    for fam in fams:
        try:
            spec = family_spec(fam)
        except Exception:
            continue
        res = find_best_in_family(spec, bars, config)
        if res is None:
            continue
        params, s, netr = res
        if netr > best_score:
            best_score = netr
            best_overall = (fam, params)
    return best_overall


def build_htf_filter(htf_bars, ema_period=50):
    if len(htf_bars) < ema_period:
        return None
    closes = [b.close for b in htf_bars]
    alpha = 2 / (ema_period + 1)
    ema = [closes[0]]
    for c in closes[1:]:
        ema.append(alpha * c + (1 - alpha) * ema[-1])
    times = [b.timestamp for b in htf_bars]
    trends = []
    for i in range(len(htf_bars)):
        if i < ema_period:
            trends.append("flat")
            continue
        c, e = closes[i], ema[i]
        e_prev = ema[i - 5] if i >= 5 else ema[0]
        if c > e and e > e_prev:
            trends.append("up")
        elif c < e and e < e_prev:
            trends.append("down")
        else:
            trends.append("flat")

    def lookup(ts):
        lo, hi = 0, len(times) - 1
        if hi < 0 or ts < times[0]:
            return "flat"
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if times[mid] <= ts:
                lo = mid
            else:
                hi = mid - 1
        return trends[lo]

    def _filter(bars, idx, side):
        ts = bars[idx].timestamp
        t = lookup(ts)
        if side is Side.LONG:
            return t == "up"
        return t == "down"

    return _filter


def main():
    bars_15m = load_bars_from_csv("data/xauusd_5y/xauusd_5y_15m.csv")
    bars_240m = load_bars_from_csv("data/xauusd_5y/xauusd_5y_240m.csv")
    config = BacktestConfig(slippage_bps=2.0, commission_per_trade=1.0)

    splits = [
        ("A", datetime(2023, 5, 4, tzinfo=timezone.utc), datetime(2024, 11, 4, tzinfo=timezone.utc), datetime(2025, 5, 4, tzinfo=timezone.utc)),
        ("B", datetime(2023, 11, 4, tzinfo=timezone.utc), datetime(2025, 5, 4, tzinfo=timezone.utc), datetime(2025, 11, 4, tzinfo=timezone.utc)),
        ("C", datetime(2024, 5, 4, tzinfo=timezone.utc), datetime(2025, 11, 4, tzinfo=timezone.utc), datetime(2026, 5, 4, tzinfo=timezone.utc)),
    ]

    results = []
    print(f"{'split':>5} {'champions':40s} {'n':>4} {'win%':>6} {'PF':>6} {'avgR':>7} {'netR(test)':>11}")
    print("-" * 90)

    for label, lo, mid, hi in splits:
        train = [b for b in bars_15m if lo <= b.timestamp < mid]
        test = [b for b in bars_15m if mid <= b.timestamp <= hi]
        train_yrs = (train[-1].timestamp - train[0].timestamp).days / 365.25
        test_yrs = (test[-1].timestamp - test[0].timestamp).days / 365.25

        champs = []
        for cname, fams in [("brk", CLUSTER_BREAKOUT), ("rev", CLUSTER_REVERSION), ("ict", CLUSTER_ICT)]:
            c = champion_per_cluster(train, config, fams)
            if c:
                champs.append((cname, c[0], c[1]))

        champ_str = " | ".join(f"{c}={f}" for c, f, _ in champs)
        if len(champs) < 2:
            print(f"  {label}  insufficient champions: {champ_str}")
            continue

        strategies = [family_spec(f).factory(p) for _, f, p in champs]

        # HTF filter on test window
        htf_test = [b for b in bars_240m if mid - timedelta(days=20) <= b.timestamp <= hi]
        bf = build_htf_filter(htf_test, ema_period=50)
        if bf is None:
            print(f"  {label}  HTF filter unavailable")
            continue

        res = run_ensemble_backtest(test, strategies, config, gate_min=2, bar_filter=bf)
        s = summarize_backtest(res.backtest)
        pf = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "  inf"
        netr = s.average_r * s.total_trades / max(0.1, test_yrs)
        results.append((label, s.profit_factor, s.total_trades, s.average_r, netr, champ_str))
        print(f"  {label}  {champ_str:40.40s} {s.total_trades:>4d} {s.win_rate:>5.1%} {pf:>6s} {s.average_r:>+7.3f} {netr:>+11.2f}")

    print("\n=== ROBUSTNESS ===")
    n_pass = sum(1 for r in results if r[1] >= 1.0)
    n_strong = sum(1 for r in results if r[1] >= 1.2)
    print(f"  splits tested: {len(results)}")
    print(f"  PF >= 1.0: {n_pass}/{len(results)}")
    print(f"  PF >= 1.2: {n_strong}/{len(results)}")
    print(f"  total trades: {sum(r[2] for r in results)}")


if __name__ == "__main__":
    main()
