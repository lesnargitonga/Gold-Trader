"""Holdout evaluation for MacroRegimeContinuationStrategy.

Tests the macro-augmented mining sweep's top theme — long gold under
falling real yields, calm VIX, consolidating DXY — as a tradable rule.

Run from repo root:

    PYTHONPATH=src .venv/bin/python scripts/eval_macro_regime_continuation.py
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
from gold_trader.strategies.macro_regime_continuation import (  # noqa: E402
    MacroRegimeContinuationStrategy,
)


@dataclass(frozen=True)
class MRParams:
    real_yield_max_change_bps: float
    dxy_max_abs_change_pct: float
    vix_max_change_abs: float
    use_fedfunds_relax: bool
    block_stagflation: bool
    stop_atr_mult: float
    risk_reward: float
    require_bullish_close: bool


def make_factory(macro):
    def _factory(p: MRParams) -> MacroRegimeContinuationStrategy:
        return MacroRegimeContinuationStrategy(
            macro=macro,
            real_yield_max_change_bps=p.real_yield_max_change_bps,
            dxy_max_abs_change_pct=p.dxy_max_abs_change_pct,
            vix_max_change_abs=p.vix_max_change_abs,
            use_fedfunds_relax=p.use_fedfunds_relax,
            block_stagflation=p.block_stagflation,
            stop_atr_mult=p.stop_atr_mult,
            risk_reward=p.risk_reward,
            require_bullish_close=p.require_bullish_close,
        )
    return _factory


def _build_grid() -> list[MRParams]:
    grid: list[MRParams] = []
    for ry in (0.0, -2.0, -5.0):       # real-yield must be ≤ this many bps over 5d
        for dxy in (0.30, 0.50, 1.00):  # DXY 20d % change ceiling
            for vix in (1.0, 1.5, 3.0):
                for ff in (False, True):
                    for stag in (True,):
                        for sm in (1.0, 1.5):
                            for rr in (1.5, 2.0, 2.5):
                                for bull in (True, False):
                                    grid.append(MRParams(
                                        real_yield_max_change_bps=ry,
                                        dxy_max_abs_change_pct=dxy,
                                        vix_max_change_abs=vix,
                                        use_fedfunds_relax=ff,
                                        block_stagflation=stag,
                                        stop_atr_mult=sm,
                                        risk_reward=rr,
                                        require_bullish_close=bull,
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
    ap.add_argument("--n-permutations", type=int, default=5000)
    ap.add_argument("--holdout-fraction", type=float, default=1 / 3)
    ap.add_argument("--min-train-trades", type=int, default=5)
    args = ap.parse_args()

    bars = load_bars_from_csv(args.csv_path)
    if not bars:
        print(f"no bars in {args.csv_path}", file=sys.stderr)
        return 2
    print(f"loaded {len(bars)} bars ({bars[0].timestamp} → {bars[-1].timestamp})")

    macro = load_macro_frame(args.macro_cache_dir)
    print(f"macro frame: {sorted(macro.names())}")
    if not {"real10y", "dxy", "vix"}.issubset(macro.names()):
        print("ERROR: required series missing (need real10y, dxy, vix)", file=sys.stderr)
        return 2

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
        strategy_factory=make_factory(macro),
        config=config,
        holdout_fraction=args.holdout_fraction,
        min_train_trades=args.min_train_trades,
        n_permutations=args.n_permutations,
        family="macro_regime_continuation",
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
