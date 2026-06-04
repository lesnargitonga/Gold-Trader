"""Honest re-evaluation of ARB at canonical params under realistic slippage
and fill-aware stop translation.  Direct response to the honest-eval
feedback that PF~1.2 thin edges are vulnerable to a few bps of execution drift.

Compares 4 configurations:
    - clean:        no slippage, legacy stops (current default)
    - slip1bp:      1 bp adverse slippage entry+exit (limit-fill realistic)
    - slip5bp:      5 bp (market-fill realistic)
    - slip5bp_fa:   5 bp + fill_aware_stops (geometry preserved relative to fill)

This is informational, not a parameter search.  The point is to see
how much of ARB's reported PF survives realistic execution friction.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.backtest.engine import run_backtest  # noqa: E402
from gold_trader.data import load_bars_from_csv  # noqa: E402
from gold_trader.models import BacktestConfig  # noqa: E402
from gold_trader.strategies.asian_range_breakout import AsianRangeBreakoutStrategy  # noqa: E402


def stats(trades: list) -> dict:
    if not trades:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "avg_r": 0.0, "total_r": 0.0}
    wins = [t for t in trades if t.pnl_r > 0]
    losses = [t for t in trades if t.pnl_r <= 0]
    gw = sum(t.pnl_r for t in wins)
    gl = -sum(t.pnl_r for t in losses)
    return {
        "n": len(trades),
        "wr": len(wins) / len(trades),
        "pf": (gw / gl) if gl > 0 else float("inf"),
        "avg_r": sum(t.pnl_r for t in trades) / len(trades),
        "total_r": sum(t.pnl_r for t in trades),
    }


def main() -> int:
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "data/xauusd_full_15m.csv"
    bars = load_bars_from_csv(csv_path)
    print(f"bars: {len(bars)}")
    strat = AsianRangeBreakoutStrategy(
        atr_period=10, risk_reward=2.5, max_spread=1.00,
        min_atr_threshold=0.0,
    )
    base = BacktestConfig(starting_equity=10_000.0, commission_per_trade=10.0)
    configs = {
        "clean":        base,
        "slip1bp":      replace(base, slippage_bps=1.0),
        "slip5bp":      replace(base, slippage_bps=5.0),
        "slip5bp_fa":   replace(base, slippage_bps=5.0, fill_aware_stops=True),
    }
    print(f"{'config':<14} {'n':>4} {'wr':>7} {'pf':>7} {'avg_r':>9} {'total_r':>9}")
    for name, cfg in configs.items():
        r = run_backtest(bars, strat, cfg)
        s = stats(list(r.trades))
        pf = f"{s['pf']:.3f}" if s["pf"] != float("inf") else "inf"
        print(
            f"{name:<14} {s['n']:>4d} {s['wr']:>6.1%} {pf:>7} "
            f"{s['avg_r']:>+9.4f} {s['total_r']:>+9.2f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
