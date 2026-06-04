"""Phase 4: Volatility-regime gate sweep on ARB.

Holds the canonical ARB winner params fixed and grid-searches only the
(min_atr_threshold, max_atr_threshold) pair to find the regime band that
maximises holdout PF.  This is a defensible, low-DOF tweak: 25 combos vs a
naive cross-product of 40 000+.

Usage
-----
    python scripts/phase4_regime_sweep.py data/xauusd_full_15m.csv

Output
------
A formatted table of (min, max, n_trades, holdout_pf, total_return) sorted by
holdout PF desc, plus the previous baseline for comparison.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make src/ importable when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest.engine import run_backtest
from gold_trader.backtest.metrics import summarize_backtest
from gold_trader.data import load_bars_from_csv
from gold_trader.models import BacktestConfig
from gold_trader.strategies import AsianRangeBreakoutStrategy


# Canonical post-fix winner params (from the 2026-05-07 holdout-eval run).
BASE_PARAMS = dict(
    atr_period=10,
    risk_reward=2.5,
    max_spread=1.0,
    min_breakout_atr=0.05,
    min_range_atr=0.2,
    min_asian_bars=3,
    min_atr_threshold=0.0,
    max_atr_threshold=0.0,
)


def _summarise(bars, params: dict) -> tuple[int, float, float, float]:
    s = AsianRangeBreakoutStrategy(**params)
    cfg = BacktestConfig(commission_per_trade=10.0, kill_switch_drawdown_fraction=None)
    res = run_backtest(bars, s, cfg)
    summ = summarize_backtest(res)
    pf = summ.profit_factor if summ.profit_factor != float("inf") else 999.0
    return summ.total_trades, pf, summ.total_return, summ.max_drawdown


def main(csv_path: str) -> None:
    bars = load_bars_from_csv(csv_path)
    n = len(bars)
    holdout_start = int(n * (2 / 3))
    train_bars = bars[:holdout_start]
    holdout_bars = bars[holdout_start:]
    print(f"csv: {csv_path}")
    print(f"total_bars={n}  train_bars={len(train_bars)}  holdout_bars={len(holdout_bars)}")

    # Sweep grid: ATR is in dollar terms on XAUUSD 15m; typical range ~1–10.
    min_floors = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    max_ceilings = [0.0, 6.0, 8.0, 10.0, 12.0, 15.0]   # 0.0 = disabled

    # Baseline (no regime gate).
    base_train = _summarise(train_bars, BASE_PARAMS)
    base_holdout = _summarise(holdout_bars, BASE_PARAMS)
    print(f"\nBASELINE (no regime gate):")
    print(f"  train  : trades={base_train[0]:>4}  pf={base_train[1]:.4f}  ret={base_train[2]:+.2%}  dd={base_train[3]:.2%}")
    print(f"  holdout: trades={base_holdout[0]:>4}  pf={base_holdout[1]:.4f}  ret={base_holdout[2]:+.2%}  dd={base_holdout[3]:.2%}")

    # Sweep — select winner on TRAIN, then report on HOLDOUT.  This avoids
    # holdout-luck cherry-picking.
    rows = []
    for mn in min_floors:
        for mx in max_ceilings:
            if mx > 0.0 and mx <= mn:
                continue  # invalid window
            params = {**BASE_PARAMS, "min_atr_threshold": mn, "max_atr_threshold": mx}
            tr_trades, tr_pf, tr_ret, tr_dd = _summarise(train_bars, params)
            ho_trades, ho_pf, ho_ret, ho_dd = _summarise(holdout_bars, params)
            rows.append((mn, mx, tr_trades, tr_pf, tr_ret, ho_trades, ho_pf, ho_ret, ho_dd))

    # Train-best selection (fair).
    rows_with_train_trades = [r for r in rows if r[2] >= 30]
    rows_with_train_trades.sort(key=lambda r: r[3], reverse=True)
    print(f"\n=== Top 10 regime configs ranked by TRAIN PF (with >=30 train trades) ===")
    print(f"{'min_atr':>8} {'max_atr':>8} | {'tr_n':>5} {'tr_pf':>7} {'tr_ret':>8} | {'ho_n':>5} {'ho_pf':>7} {'ho_ret':>8} {'ho_dd':>7}")
    print("-" * 88)
    for r in rows_with_train_trades[:10]:
        mn, mx, trn, trpf, trret, hon, hopf, horet, hodd = r
        print(f"{mn:>8.1f} {mx:>8.1f} | {trn:>5} {trpf:>7.3f} {trret:>+8.2%} | {hon:>5} {hopf:>7.3f} {horet:>+8.2%} {hodd:>7.2%}")

    if rows_with_train_trades:
        winner = rows_with_train_trades[0]
        mn, mx, trn, trpf, trret, hon, hopf, horet, hodd = winner
        print(f"\nTRAIN-SELECTED WINNER: min_atr={mn}, max_atr={mx}")
        print(f"  train  : n={trn} pf={trpf:.4f} ret={trret:+.2%}")
        print(f"  holdout: n={hon} pf={hopf:.4f} ret={horet:+.2%} dd={hodd:.2%}")
        print(f"\nvs baseline holdout pf={base_holdout[1]:.4f} ret={base_holdout[2]:+.2%} dd={base_holdout[3]:.2%}")
        delta_pf = hopf - base_holdout[1]
        print(f"holdout PF delta: {delta_pf:+.4f}")
        if delta_pf > 0.05 and hon >= 50:
            print("VERDICT: regime gate IMPROVES holdout PF meaningfully.")
        elif delta_pf > 0.0:
            print("VERDICT: regime gate marginally improves holdout (within noise band).")
        else:
            print("VERDICT: regime gate does NOT improve holdout — keep current params.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/xauusd_full_15m.csv")
