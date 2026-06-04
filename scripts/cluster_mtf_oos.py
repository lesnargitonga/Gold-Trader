"""Cluster + multi-timeframe filter + true OOS test.

Two flaws fixed simultaneously:
  1. Cluster-aware: only same-thesis strategies vote together
  2. HTF context: 240m trend (close vs EMA50) gates direction

Protocol (true OOS, no peeking):
  TRAIN: 2023-05 .. 2025-05 (tune best params per cluster)
  TEST:  2025-05 .. 2026-05 (apply locked params + HTF filter)

Three sub-strategies tested:
  A. Best breakout, taken only when 240m trend == LONG side direction
  B. Best mean-reversion, taken only when 240m is FLAT (no trend)
  C. Best ICT/SMC, taken only when 240m trend agrees with side

Why these alignments?
  - Breakouts work in trending HTF (continuation thesis)
  - Mean-reversion works in flat/range HTF (price reverts to range mid)
  - ICT/SMC works best when HTF agrees (institutional move follow-through)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections.abc import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import (
    run_backtest, run_ensemble_backtest, summarize_backtest,
)
from gold_trader.data import load_bars_from_csv
from gold_trader.models import BacktestConfig, MarketBar, Side
from gold_trader.research.family_grids import family_spec


CLUSTER_BREAKOUT = [
    "opening_range_breakout", "asian_range_breakout", "london_breakout",
    "previous_day_breakout", "ny_session_breakout", "compression_breakout",
    "momentum_burst", "session_continuation", "trend_pullback",
]
CLUSTER_REVERSION = ["asian_range_fade", "ny_close_compression", "rsi_divergence"]
CLUSTER_ICT = ["fair_value_gap", "inversion_fair_value_gap", "liquidity_sweep"]


def find_best_in_family(spec, bars, config, subsample=30, min_trades=20):
    full = list(spec.grid)
    if len(full) > subsample:
        stride = max(1, len(full) // subsample)
        grid = full[::stride][:subsample]
    else:
        grid = full
    best = None
    best_score = -1e9
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
        # Score: penalize low PF, reward netR/yr
        if s.profit_factor < 1.05:
            continue
        netr = s.average_r * (s.total_trades / years)
        if netr > best_score:
            best_score = netr
            best = (params, s, netr)
    return best


def build_htf_trend_lookup(htf_bars: Sequence[MarketBar], ema_period: int = 50):
    """Compute 240m EMA50 trend at each HTF bar.  Returns sorted timestamp
    list and parallel trend list ('up','down','flat')."""
    if len(htf_bars) < ema_period:
        return [], []
    closes = [b.close for b in htf_bars]
    # Simple EMA
    alpha = 2 / (ema_period + 1)
    ema = [closes[0]]
    for c in closes[1:]:
        ema.append(alpha * c + (1 - alpha) * ema[-1])
    # Direction: close above EMA + EMA rising = up; below + falling = down; else flat
    times = [b.timestamp for b in htf_bars]
    trends = []
    for i in range(len(htf_bars)):
        if i < ema_period:
            trends.append("flat")
            continue
        c = closes[i]
        e = ema[i]
        e_prev = ema[i - 5] if i >= 5 else ema[0]
        if c > e and e > e_prev:
            trends.append("up")
        elif c < e and e < e_prev:
            trends.append("down")
        else:
            trends.append("flat")
    return times, trends


def make_htf_filter(htf_times, htf_trends, mode):
    """Build a bar_filter callable that gates entries by 240m trend.

    mode: 'follow' = only trade in direction of HTF trend
          'fade'   = only trade when HTF is flat (range)
    """
    # Pre-build lookup function: for any bar timestamp, find latest HTF
    # bar at-or-before it and return its trend.
    def lookup(ts):
        # Linear scan from end (caller passes monotonic ts so we cache)
        # Simple version: bisect
        lo, hi = 0, len(htf_times) - 1
        if hi < 0 or ts < htf_times[0]:
            return "flat"
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if htf_times[mid] <= ts:
                lo = mid
            else:
                hi = mid - 1
        return htf_trends[lo]

    def _filter(bars, idx, side):
        ts = bars[idx].timestamp
        trend = lookup(ts)
        if mode == "follow":
            if side is Side.LONG:
                return trend == "up"
            else:
                return trend == "down"
        elif mode == "fade":
            return trend == "flat"
        return True

    return _filter


def main():
    bars_15m = load_bars_from_csv("data/xauusd_5y/xauusd_5y_15m.csv")
    bars_240m = load_bars_from_csv("data/xauusd_5y/xauusd_5y_240m.csv")

    train_lo = datetime(2023, 5, 4, tzinfo=timezone.utc)
    train_hi = datetime(2025, 5, 4, tzinfo=timezone.utc)
    test_hi = datetime(2026, 5, 4, tzinfo=timezone.utc)

    train_15m = [b for b in bars_15m if train_lo <= b.timestamp < train_hi]
    test_15m = [b for b in bars_15m if train_hi <= b.timestamp <= test_hi]
    print(f"TRAIN: {len(train_15m)} bars 15m | TEST: {len(test_15m)} bars 15m")

    config = BacktestConfig(slippage_bps=2.0, commission_per_trade=1.0)

    # Tune best in each cluster on TRAIN
    print("\n=== TUNING (TRAIN 2023-25) ===")
    print(f"{'cluster':>15} {'family':30s} {'PF':>6} {'n':>4} {'avgR':>8} {'netR/yr':>9}")
    cluster_picks = {}
    for cname, fams in [("breakout", CLUSTER_BREAKOUT), ("reversion", CLUSTER_REVERSION), ("ict", CLUSTER_ICT)]:
        best_overall = None
        best_score = -1e9
        for fam in fams:
            try:
                spec = family_spec(fam)
            except Exception:
                continue
            res = find_best_in_family(spec, train_15m, config)
            if res is None:
                continue
            params, s, netr = res
            print(f"{cname:>15} {fam:30s} {s.profit_factor:>6.2f} {s.total_trades:>4d} {s.average_r:>+8.3f} {netr:>+9.2f}")
            if netr > best_score:
                best_score = netr
                best_overall = (fam, params, s)
        if best_overall is not None:
            cluster_picks[cname] = best_overall
            print(f"  -> {cname} CHAMPION: {best_overall[0]}")

    if not cluster_picks:
        print("No champions — abort.")
        return

    # Build HTF trend lookup for TEST window
    htf_test = [b for b in bars_240m if train_hi - timedelta(days=20) <= b.timestamp <= test_hi]
    htf_times, htf_trends = build_htf_trend_lookup(htf_test, ema_period=50)
    print(f"\nHTF (240m) trend distribution in TEST:")
    from collections import Counter
    print(f"  {Counter(htf_trends)}")

    follow_filter = make_htf_filter(htf_times, htf_trends, mode="follow")
    fade_filter = make_htf_filter(htf_times, htf_trends, mode="fade")

    # Apply locked champions to TEST, with and without HTF filter
    print("\n=== TEST (2025-05..2026-05, locked params, costs included) ===")
    print(f"{'cluster':>10} {'champion':30s} {'mode':>12} {'n':>4} {'win%':>6} {'PF':>6} {'avgR':>7} {'netR/yr':>8}")
    locked_strategies = []
    for cname, (fam, params, _) in cluster_picks.items():
        spec = family_spec(fam)
        strat = spec.factory(params)
        locked_strategies.append((cname, strat))
        # 1. plain (no HTF)
        bt = run_backtest(test_15m, strat, config)
        s = summarize_backtest(bt)
        years = 1.0
        netr = s.average_r * s.total_trades / years
        pf = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "  inf"
        print(f"{cname:>10} {fam:30s} {'plain':>12} {s.total_trades:>4d} {s.win_rate:>5.1%} {pf:>6s} {s.average_r:>+7.3f} {netr:>+8.2f}")
        # 2. With HTF filter (single-strategy ensemble + filter)
        bf = follow_filter if cname in ("breakout", "ict") else fade_filter
        ftext = "htf-follow" if cname in ("breakout", "ict") else "htf-fade"
        res = run_ensemble_backtest(test_15m, [strat], config, gate_min=1, bar_filter=bf)
        s2 = summarize_backtest(res.backtest)
        netr2 = s2.average_r * s2.total_trades / years
        pf2 = f"{s2.profit_factor:.2f}" if s2.profit_factor != float("inf") else "  inf"
        print(f"{cname:>10} {fam:30s} {ftext:>12} {s2.total_trades:>4d} {s2.win_rate:>5.1%} {pf2:>6s} {s2.average_r:>+7.3f} {netr2:>+8.2f}")

    # Combined ensemble of cluster champions WITH HTF filter
    print("\n=== COMBINED ENSEMBLE (3 cluster champions + HTF) ===")
    strats_only = [s for _, s in locked_strategies]

    # combined filter: each side accepted if its CLUSTER's HTF preference passes
    # but we only have one strategy per cluster, so a per-bar filter must
    # know which strategy fires.  Since clusters all pick "trend follow" or
    # "fade flat", build a unified filter: LONG ok if (any cluster's mode
    # would allow it).  Simpler: gate=1 + per-cluster filter applied
    # separately is what we already showed individually.  For combined
    # ensemble, apply BOTH conditions — favour HTF-follow because most
    # strategies are breakout-flavoured.
    res = run_ensemble_backtest(test_15m, strats_only, config, gate_min=1, bar_filter=follow_filter)
    s = summarize_backtest(res.backtest)
    pf = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "  inf"
    print(f"  gate=1 + htf-follow: n={s.total_trades} win={s.win_rate:.1%} PF={pf} avgR={s.average_r:+.3f}")

    res2 = run_ensemble_backtest(test_15m, strats_only, config, gate_min=2, bar_filter=follow_filter)
    s2 = summarize_backtest(res2.backtest)
    pf2 = f"{s2.profit_factor:.2f}" if s2.profit_factor != float("inf") else "  inf"
    print(f"  gate=2 + htf-follow: n={s2.total_trades} win={s2.win_rate:.1%} PF={pf2} avgR={s2.average_r:+.3f}")


if __name__ == "__main__":
    main()
