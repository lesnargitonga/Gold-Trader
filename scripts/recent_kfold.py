"""Focused recent-3y validation (2023-05 -> 2026-05).

The 5y k-fold revealed gate=6 is regime-dependent: PF<1 in folds 1-2
(2021-23, rate-hike chop) and PF>=1 in folds 3-5 (2023-26, dovish/cuts
regime).  Recent regime is what matters for live deployment.

This script:
  1. Carves recent 3y from data/xauusd_5y/xauusd_5y_15m.csv
  2. Splits into 3 contiguous 1y out-of-sample folds
  3. Runs gate sweep with realistic costs
  4. Adds a DAILY-macro-switch overlay (real10y 20d trend, dxy 20d,
     vix level) — coarse, not per-15m-bar
  5. Compares plain vs daily-switch by fold + aggregate
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import run_ensemble_backtest, summarize_backtest
from gold_trader.data import load_bars_from_csv, load_macro_frame
from gold_trader.models import BacktestConfig, Side
from gold_trader.research.family_grids import (
    all_self_contained_families, family_spec,
)


def build_strategies():
    return [family_spec(f).factory(family_spec(f).grid[0])
            for f in all_self_contained_families()]


def build_daily_switch(macro):
    """Return per-bar predicate: is the regime ON for long-only gold today?

    Switch is coarse and stable: real10y 20d-change <= +5bps,
    dxy 20d-pct <= +1.5%, vix <= 28.  All three are checked at the
    UTC midnight preceding the bar (no intraday noise).
    """
    real = macro.get("real10y")
    dxy = macro.get("dxy")
    vix = macro.get("vix")
    cache: dict[str, bool] = {}

    def _on(_bars, idx, side):
        # Long-only switch.  Short side: pass through (we'll let it trade)
        # — we are not optimising shorts here.
        if side is Side.SHORT:
            return True
        ts = _bars[idx].timestamp
        # Snap to UTC midnight to cache per-day.
        day = ts.date().isoformat()
        if day in cache:
            return cache[day]
        ok = True
        midnight = datetime.combine(ts.date(), datetime.min.time(), tzinfo=timezone.utc)
        if real is not None:
            d = real.change(midnight, lookback_days=20)
            if d is None or d * 100.0 > 5.0:  # bps; rising real yields kill gold
                ok = False
        if ok and dxy is not None:
            d = dxy.pct_change(midnight, lookback_days=20)
            if d is None or d * 100.0 > 1.5:
                ok = False
        if ok and vix is not None:
            v = vix.as_of(midnight)
            if v is None or v > 28.0:
                ok = False
        cache[day] = ok
        return ok

    return _on


def main():
    csv_path = "data/xauusd_5y/xauusd_5y_15m.csv"
    print(f"loading {csv_path} ...")
    all_bars = load_bars_from_csv(csv_path)
    cutoff = datetime(2023, 5, 4, tzinfo=timezone.utc)
    bars = [b for b in all_bars if b.timestamp >= cutoff]
    print(f"  recent 3y: {len(bars)} bars  {bars[0].timestamp} .. {bars[-1].timestamp}")

    config = BacktestConfig(slippage_bps=2.0, commission_per_trade=1.0)
    strategies = build_strategies()
    macro = load_macro_frame("data/macro")
    switch = build_daily_switch(macro)

    # 3 contiguous 1y folds
    folds = []
    span = (bars[-1].timestamp - bars[0].timestamp) / 3
    for i in range(3):
        lo = bars[0].timestamp + span * i
        hi = bars[0].timestamp + span * (i + 1) if i < 2 else bars[-1].timestamp + timedelta(seconds=1)
        chunk = [b for b in bars if lo <= b.timestamp < hi]
        folds.append((lo, hi, chunk))

    # Pre-compute switch ON-fraction per fold for transparency.
    print("\n=== daily-switch ON fraction by fold ===")
    for i, (lo, hi, chunk) in enumerate(folds, start=1):
        days = sorted({b.timestamp.date() for b in chunk})
        on_days = sum(1 for d in days if switch(
            [type("X", (), {"timestamp": datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)})()],
            0, Side.LONG))
        print(f"  fold {i} ({lo.date()}..{hi.date()}): {on_days}/{len(days)} days ON ({on_days/len(days):.1%})")

    print(
        "\nfold | period            | gate | mode      | n  | win%  | PF    | avgR    | eq"
    )
    print("-" * 90)
    summary: dict[tuple[int, str], list[tuple[float, int, float]]] = {}
    for i, (lo, hi, chunk) in enumerate(folds, start=1):
        for gm in (5, 6, 7):
            for label, bf in [("plain", None), ("daily-sw", switch)]:
                res = run_ensemble_backtest(
                    chunk, strategies, config, gate_min=gm, bar_filter=bf,
                )
                s = summarize_backtest(res.backtest)
                pf = s.profit_factor
                pf_disp = f"{pf:5.2f}" if pf != float("inf") else "  inf"
                print(
                    f"  {i}  | {lo.date()}..{hi.date()} | {gm:>4} | {label:9s} | "
                    f"{s.total_trades:2d} | {s.win_rate:5.1%} | {pf_disp} | "
                    f"{s.average_r:+.3f} | {res.backtest.ending_equity:.0f}"
                )
                summary.setdefault((gm, label), []).append((pf, s.total_trades, s.average_r))

    print("\n=== AGGREGATE (recent 3y) ===")
    print("gate | mode      | folds_PF>=1.0 | folds_PF>=1.2 | total_trades | avg_avgR")
    for (gm, label), xs in sorted(summary.items()):
        finite = [x for x in xs if x[0] != float("inf")]
        n_pass = sum(1 for x in xs if x[0] >= 1.0)
        n_strong = sum(1 for x in xs if x[0] >= 1.2)
        total = sum(x[1] for x in xs)
        avg_r = sum(x[2] * x[1] for x in xs) / total if total else 0.0
        print(
            f"{gm:>4} | {label:9s} | {n_pass}/3           | {n_strong}/3           | "
            f"{total:>3}          | {avg_r:+.3f}"
        )


if __name__ == "__main__":
    main()
