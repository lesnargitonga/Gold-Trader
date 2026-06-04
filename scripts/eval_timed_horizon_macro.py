"""Holdout evaluation for TimedHorizonMacroRegimeStrategy.

Tests whether the macro-regime conjunction's avg_R signal becomes
tradable when we exit at a fixed bar horizon (matching the miner's
measurement) rather than via stop/target.

Run from repo root:

    PYTHONPATH=src .venv/bin/python scripts/eval_timed_horizon_macro.py [csv]
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.data import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.models import BacktestConfig  # noqa: E402
from gold_trader.research.holdout import run_holdout_evaluation  # noqa: E402
from gold_trader.strategies.timed_horizon_macro_regime import (  # noqa: E402
    TimedHorizonMacroRegimeStrategy,
)


@dataclass(frozen=True)
class THParams:
    real_yield_max_change_bps: float
    vix_max_change_abs: float
    require_dxy_flat: bool
    dxy_max_abs_change_pct: float
    require_bullish_close: bool
    once_per_day: bool


def make_factory(macro):
    def _factory(p: THParams) -> TimedHorizonMacroRegimeStrategy:
        return TimedHorizonMacroRegimeStrategy(
            macro=macro,
            real_yield_max_change_bps=p.real_yield_max_change_bps,
            vix_max_change_abs=p.vix_max_change_abs,
            require_dxy_flat=p.require_dxy_flat,
            dxy_max_abs_change_pct=p.dxy_max_abs_change_pct,
            require_bullish_close=p.require_bullish_close,
            once_per_day=p.once_per_day,
        )
    return _factory


def _build_grid() -> list[THParams]:
    grid: list[THParams] = []
    for ry in (0.0, -2.0, -5.0):
        for vix in (1.5, 3.0, 5.0):
            for dxy_flat, dxy_pct in ((True, 0.5), (True, 1.0), (False, 99.0)):
                for bull in (False, True):
                    for opd in (True, False):
                        grid.append(THParams(
                            real_yield_max_change_bps=ry,
                            vix_max_change_abs=vix,
                            require_dxy_flat=dxy_flat,
                            dxy_max_abs_change_pct=dxy_pct,
                            require_bullish_close=bull,
                            once_per_day=opd,
                        ))
    return grid


def _format_summary(s, label: str) -> str:
    return "\n".join([
        f"  {label}",
        f"    trades:        {s.total_trades}",
        f"    win_rate:      {s.win_rate:.1%}",
        f"    avg_r:         {s.average_r:.4f}",
        f"    profit_factor: {s.profit_factor:.4f}",
        f"    total_return:  {s.total_return:.2%}",
        f"    max_dd:        {s.max_drawdown:.2%}",
    ])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path", nargs="?", default="data/xauusd_full_60m.csv")
    ap.add_argument("--macro-cache-dir", default="data/macro")
    ap.add_argument("--commission", type=float, default=10.0)
    ap.add_argument("--n-permutations", type=int, default=3000)
    ap.add_argument(
        "--horizon-bars",
        type=int,
        default=16,
        help="max_hold_bars (=mining horizon).  60m × 16 = 16h.",
    )
    args = ap.parse_args()

    bars = load_bars_from_csv(args.csv_path)
    if not bars:
        print(f"no bars in {args.csv_path}", file=sys.stderr)
        return 2
    print(f"loaded {len(bars)} bars ({bars[0].timestamp} → {bars[-1].timestamp})")

    macro = load_macro_frame(args.macro_cache_dir)
    print(f"macro frame: {sorted(macro.names())}")

    grid = _build_grid()
    print(f"grid size: {len(grid)}")
    print(f"horizon (max_hold_bars): {args.horizon_bars}")

    config = BacktestConfig(
        starting_equity=10_000.0,
        commission_per_trade=args.commission,
        max_hold_bars=args.horizon_bars,
    )

    print("running holdout evaluation ...")
    result = run_holdout_evaluation(
        bars=bars,
        param_grid=grid,
        strategy_factory=make_factory(macro),
        config=config,
        holdout_fraction=1 / 3,
        min_train_trades=8,
        n_permutations=args.n_permutations,
        family="timed_horizon_macro_regime",
        family_name="",
        n_workers=1,
    )

    print()
    print(f"family:        {result.family}")
    print(f"best_params:   {result.best_params}")
    print(f"train_pf:      {result.train_pf:.4f}")
    print()
    print("--- held-out out-of-sample ---")
    print(_format_summary(result.holdout_summary, "holdout backtest"))
    print(f"  holdout_perm_p:  {result.holdout_permutation.p_value:.4f}")
    print(f"  holdout_perm_v:  {result.holdout_permutation.verdict}")
    print()
    print("--- true walk-forward (train) ---")
    print(f"  windows:           {result.true_walk_forward.window_count}")
    print(
        f"  positive_ratio:    "
        f"{result.true_walk_forward.positive_window_ratio:.0%}"
    )
    print(f"  avg_r:             {result.true_walk_forward.average_r:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
