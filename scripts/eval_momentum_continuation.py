"""Holdout evaluation for MomentumContinuationStrategy.

Validates the empirical pattern-mining result (`near_20_high & trend_up`
is a robust long-bias edge in XAUUSD) against the formal backtest
machinery: walk-forward param search on 2/3 train slice, single param
pick, hard holdout on 1/3, permutation test on holdout trades.

Run from repo root:

    PYTHONPATH=src .venv/bin/python scripts/eval_momentum_continuation.py
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.data import load_bars_from_csv  # noqa: E402
from gold_trader.models import BacktestConfig  # noqa: E402
from gold_trader.research.holdout import run_holdout_evaluation  # noqa: E402
from gold_trader.strategies.momentum_continuation import (  # noqa: E402
    MomentumContinuationStrategy,
)


@dataclass(frozen=True)
class MCParams:
    atr_period: int
    high_lookback: int
    stop_lookback: int
    risk_reward: float
    near_atr: float
    stop_atr_buffer: float
    min_atr_threshold: float
    max_atr_threshold: float


def _factory(p: MCParams) -> MomentumContinuationStrategy:
    return MomentumContinuationStrategy(
        atr_period=p.atr_period,
        high_lookback=p.high_lookback,
        stop_lookback=p.stop_lookback,
        risk_reward=p.risk_reward,
        near_atr=p.near_atr,
        stop_atr_buffer=p.stop_atr_buffer,
        min_atr_threshold=p.min_atr_threshold,
        max_atr_threshold=p.max_atr_threshold,
    )


def _build_grid() -> list[MCParams]:
    grid: list[MCParams] = []
    for atr_period in (10, 14):
        for hl in (15, 20, 30):
            for sl in (10, 15):
                for rr in (1.5, 2.0, 2.5):
                    for near in (0.20, 0.30, 0.50):
                        for buf in (0.3, 0.5):
                            grid.append(MCParams(
                                atr_period=atr_period,
                                high_lookback=hl,
                                stop_lookback=sl,
                                risk_reward=rr,
                                near_atr=near,
                                stop_atr_buffer=buf,
                                min_atr_threshold=0.0,
                                max_atr_threshold=0.0,
                            ))
    return grid


def _format_summary(s, label: str) -> str:
    lines = [
        f"  {label}",
        f"    trades:        {s.total_trades}",
        f"    win_rate:      {s.win_rate:.1%}",
        f"    avg_r:         {s.average_r:.4f}",
        f"    profit_factor: {s.profit_factor:.4f}",
        f"    total_return:  {s.total_return:.2%}",
        f"    max_dd:        {s.max_drawdown:.2%}",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path", nargs="?",
                    default="data/xauusd_full_15m.csv")
    ap.add_argument("--commission", type=float, default=10.0)
    ap.add_argument("--n-permutations", type=int, default=5000)
    ap.add_argument("--holdout-fraction", type=float, default=1 / 3)
    args = ap.parse_args()

    bars = load_bars_from_csv(args.csv_path)
    if not bars:
        print(f"no bars in {args.csv_path}", file=sys.stderr)
        return 2
    print(
        f"loaded {len(bars)} bars "
        f"({bars[0].timestamp} → {bars[-1].timestamp})"
    )

    grid = _build_grid()
    print(f"grid size: {len(grid)}")

    config = BacktestConfig(
        starting_equity=10_000.0,
        commission_per_trade=args.commission,
    )

    print("running holdout evaluation ...")
    result = run_holdout_evaluation(
        bars=bars,
        param_grid=grid,
        strategy_factory=_factory,
        config=config,
        holdout_fraction=args.holdout_fraction,
        min_train_trades=5,
        n_permutations=args.n_permutations,
        family="momentum_continuation",
        family_name="",
        n_workers=1,
    )

    print()
    print(f"family:        {result.family}")
    print(f"total_bars:    {len(bars)}")
    print(f"train_bars:    {result.train_bars}")
    print(f"holdout_bars:  {result.holdout_bars}")
    print(f"best_params:   {result.best_params}")
    print(f"train_pf:      {result.train_pf:.4f}")
    print()
    print("--- held-out out-of-sample ---")
    print(_format_summary(result.holdout_summary, "holdout backtest"))
    print(
        f"  holdout_perm_p:  {result.holdout_permutation.p_value:.4f}"
    )
    print(
        f"  holdout_perm_v:  {result.holdout_permutation.verdict}"
    )
    print()
    print("--- true walk-forward (train) ---")
    print(f"  windows:           {result.true_walk_forward.window_count}")
    print(
        f"  positive_ratio:    "
        f"{result.true_walk_forward.positive_window_ratio:.0%}"
    )
    print(
        f"  avg_r:             "
        f"{result.true_walk_forward.average_r:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
