"""Cluster-aware ensemble — only same-thesis strategies vote together.

Flaw discovered: the 15-strategy gate counts agreements across
philosophically-opposed strategies (breakouts + fades on the same
bar = destructive interference, not confluence).

This script defines thematic clusters and runs per-cluster ensembles
on recent 3y with realistic costs.  Only strategies that share a
thesis can confirm each other.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import run_ensemble_backtest, summarize_backtest
from gold_trader.data import load_bars_from_csv
from gold_trader.models import BacktestConfig
from gold_trader.research.family_grids import family_spec


CLUSTERS = {
    "breakout_trend": [
        "opening_range_breakout",
        "asian_range_breakout",
        "london_breakout",
        "previous_day_breakout",
        "ny_session_breakout",
        "compression_breakout",
        "momentum_burst",
        "session_continuation",
        "trend_pullback",
    ],
    "mean_reversion": [
        "asian_range_fade",
        "ny_close_compression",
        "rsi_divergence",
    ],
    "ict_smc": [
        "fair_value_gap",
        "inversion_fair_value_gap",
        "liquidity_sweep",
    ],
}


def main():
    csv_path = "data/xauusd_5y/xauusd_5y_15m.csv"
    all_bars = load_bars_from_csv(csv_path)
    cutoff = datetime(2023, 5, 4, tzinfo=timezone.utc)
    bars = [b for b in all_bars if b.timestamp >= cutoff]
    years = (bars[-1].timestamp - bars[0].timestamp).days / 365.25
    print(f"recent {years:.1f}y: {len(bars)} bars 15m, costs: 2bps + $1\n")

    config = BacktestConfig(slippage_bps=2.0, commission_per_trade=1.0)

    print(f"{'cluster':>16}  {'gate':>4}  {'gated':>5}  {'n':>4}  "
          f"{'win%':>6}  {'PF':>6}  {'avgR':>7}  {'netR/yr':>8}")
    print("-" * 75)

    for cname, fams in CLUSTERS.items():
        strategies = [family_spec(f).factory(family_spec(f).grid[0]) for f in fams]
        for gm in range(1, len(strategies) + 1):
            res = run_ensemble_backtest(bars, strategies, config, gate_min=gm)
            s = summarize_backtest(res.backtest)
            netr = s.average_r * s.total_trades / years
            pf = f"{s.profit_factor:.2f}" if s.profit_factor != float("inf") else "  inf"
            print(f"{cname:>16}  {gm:>4d}  {res.n_signals_gated_in:>5d}  "
                  f"{s.total_trades:>4d}  {s.win_rate:>5.1%}  {pf:>6s}  "
                  f"{s.average_r:>+7.3f}  {netr:>+8.2f}")
        print()


if __name__ == "__main__":
    main()
