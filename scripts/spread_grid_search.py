"""
Spread grid search on ARB training split.

Fixes max_spread as the only variable; all other params are the canonical ARB values.
Runs only on the training split (first 75% of bars) to avoid look-ahead bias.

Usage:
    .venv/bin/python scripts/spread_grid_search.py data/xauusd_full_15m.csv
"""

import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from gold_trader.data import load_bars_from_csv
from gold_trader.backtest.engine import run_backtest, BacktestConfig
from gold_trader.strategies.asian_range_breakout import AsianRangeBreakoutStrategy


def main(csv_path: str) -> None:
    all_bars = load_bars_from_csv(csv_path)
    # Match holdout.py split: holdout_fraction=1/3 → train = first 2/3
    split = int(len(all_bars) * (1.0 - 1 / 3))
    train_bars = all_bars[:split]

    print(f"Total bars: {len(all_bars)}  |  Train bars: {len(train_bars)}  |  Holdout bars: {len(all_bars) - split}")
    print()

    # Canonical ARB params — only max_spread varies
    CANONICAL = dict(
        atr_period=14,
        risk_reward=2.5,
        min_breakout_atr=0.05,
        min_range_atr=0.2,
        min_asian_bars=3,
        min_atr_threshold=10.0,
        entry_slippage_buffer=0.1,
    )

    config = BacktestConfig(commission_per_trade=10.0)

    # max_spread from 0.50 to 2.00 in 0.10 steps
    spread_values = [round(0.50 + i * 0.10, 2) for i in range(16)]

    print(f"{'max_spread':>12} {'trades':>8} {'PF':>8} {'avg_r':>8} {'win%':>8} {'max_dd%':>10}")
    print("-" * 58)

    for ms in spread_values:
        strat = AsianRangeBreakoutStrategy(max_spread=ms, **CANONICAL)
        result = run_backtest(train_bars, strat, config)
        trades = result.trades

        if not trades:
            print(f"{ms:>12.2f} {'0':>8} {'n/a':>8} {'n/a':>8} {'n/a':>8} {'n/a':>10}")
            continue

        gross_wins = sum(t.pnl for t in trades if t.pnl > 0)
        gross_losses = abs(sum(t.pnl for t in trades if t.pnl < 0))
        pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")
        avg_r = sum(t.pnl_r for t in trades) / len(trades)
        win_pct = 100.0 * sum(1 for t in trades if t.pnl > 0) / len(trades)

        # Max drawdown
        equity = config.starting_equity
        peak = equity
        max_dd = 0.0
        for t in trades:
            equity += t.pnl
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd

        print(f"{ms:>12.2f} {len(trades):>8d} {pf:>8.3f} {avg_r:>8.3f} {win_pct:>7.1f}% {max_dd:>9.1f}%")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <csv_path>")
        sys.exit(1)
    main(sys.argv[1])
