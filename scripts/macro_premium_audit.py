#!/usr/bin/env python3
"""Phase 14b — macro/external-data family audit under Phase-13 honesty bar.

Tests the four families that were excluded from premium_audit.py because
they need external data:
    - dxy_lead_lag        (DXY column already in xauusd_5y_*.csv)
    - real_yield_reversal (MacroFrame from data/macro/)
    - macro_regime_continuation (MacroFrame, hand-picked grid — no defaults)
    - timed_horizon_macro_regime (MacroFrame, hand-picked grid)

Same protocol: 5×1y folds, retail costs, sample N random params, train
Y1-Y4 / hold Y5, Bonferroni-correct over the sample size.
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import run_backtest  # noqa: E402
from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.models import BacktestConfig, MarketBar  # noqa: E402
from gold_trader.research.experiments import (  # noqa: E402
    default_dxy_lead_lag_grid,
    default_real_yield_reversal_grid,
)
from gold_trader.strategies.dxy_lead_lag import DXYLeadLagStrategy  # noqa: E402
from gold_trader.strategies.macro_regime_continuation import (  # noqa: E402
    MacroRegimeContinuationStrategy,
)
from gold_trader.strategies.real_yield_reversal import RealYieldReversalStrategy  # noqa: E402
from gold_trader.strategies.timed_horizon_macro_regime import (  # noqa: E402
    TimedHorizonMacroRegimeStrategy,
)


CONFIG = BacktestConfig(
    starting_equity=100_000.0,
    risk_fraction=0.01,
    max_hold_bars=24,
    kill_switch_drawdown_fraction=None,
    slippage_bps=2.0,
    commission_per_trade=1.0,
)
SPLITS = [
    (
        datetime(2021 + i, 5, 4, tzinfo=timezone.utc),
        datetime(2022 + i, 5, 4, tzinfo=timezone.utc),
    )
    for i in range(5)
]
TRAIN_FOLDS = [0, 1, 2, 3]
HOLDOUT_FOLD = 4
BLOCK = 10
N_RESAMPLES = 5000
MIN_TRAIN_TRADES = 20
RNG_SEED = 41


# ── hand-built grids for the families that don't have a default ────────
def _macro_regime_grid():
    grid = []
    for ry_lookback in [5, 10]:
        for ry_max_bps in [-3.0, 0.0]:    # require real-yield drop or flat-to-down
            for dxy_lookback in [10, 20]:
                for dxy_pct in [0.5, 1.0]:
                    for vix_max in [1.5, 2.5]:
                        for rr in [2.0, 2.5]:
                            grid.append(dict(
                                real_yield_lookback_days=ry_lookback,
                                real_yield_max_change_bps=ry_max_bps,
                                dxy_lookback_days=dxy_lookback,
                                dxy_max_abs_change_pct=dxy_pct,
                                vix_lookback_days=5,
                                vix_max_change_abs=vix_max,
                                risk_reward=rr,
                                stop_atr_mult=1.5,
                            ))
    return grid


def _timed_horizon_grid():
    grid = []
    for ry_lookback in [5, 10]:
        for ry_max_bps in [-3.0, 0.0]:
            for vix_max in [1.5, 2.5]:
                for dxy_pct in [0.5, 1.0]:
                    for far in [8.0, 12.0, 16.0]:
                        grid.append(dict(
                            real_yield_lookback_days=ry_lookback,
                            real_yield_max_change_bps=ry_max_bps,
                            vix_max_change_abs=vix_max,
                            dxy_max_abs_change_pct=dxy_pct,
                            far_atr_mult=far,
                        ))
    return grid


# ── factory closures (depend on macro frame) ───────────────────────────
def _make_factory(family: str, macro_frame=None):
    if family == "dxy_lead_lag":
        return lambda p: DXYLeadLagStrategy(
            lookback=p.lookback, min_dxy_drop=p.min_dxy_drop,
            max_gold_response=p.max_gold_response, atr_period=p.atr_period,
            stop_atr_mult=p.stop_atr_mult, risk_reward=p.risk_reward,
            max_spread=p.max_spread, min_atr_threshold=p.min_atr_threshold,
        )
    if family == "real_yield_reversal":
        return lambda p: RealYieldReversalStrategy(
            macro=macro_frame,
            yield_lookback_days=p.yield_lookback_days,
            min_yield_move_bps=p.min_yield_move_bps,
            atr_period=p.atr_period, stop_atr_mult=p.stop_atr_mult,
            risk_reward=p.risk_reward, enter_longs=p.enter_longs,
            enter_shorts=p.enter_shorts, max_spread=p.max_spread,
            min_atr_threshold=p.min_atr_threshold,
        )
    if family == "macro_regime_continuation":
        return lambda p: MacroRegimeContinuationStrategy(macro=macro_frame, **p)
    if family == "timed_horizon_macro_regime":
        return lambda p: TimedHorizonMacroRegimeStrategy(macro=macro_frame, **p)
    raise ValueError(family)


def _slice(bars: Sequence[MarketBar], lo: datetime, hi: datetime):
    return [b for b in bars if lo <= b.timestamp <= hi]


def _trades(strategy, bars):
    return [t.pnl_r for t in run_backtest(bars, strategy, CONFIG).trades]


def _fold_metrics(rs):
    n = len(rs); pnl = sum(rs)
    win = sum(r for r in rs if r > 0); loss = sum(-r for r in rs if r < 0)
    pf = win/loss if loss > 0 else (99.0 if win > 0 else 0.0)
    return n, pnl, pf


def _bootstrap_p(all_rs, rng):
    if len(all_rs) < BLOCK*2:
        return float("nan")
    n = len(all_rs); n_blocks = (n + BLOCK - 1)//BLOCK
    samples = []
    last = n - BLOCK
    for _ in range(N_RESAMPLES):
        out = []
        for _ in range(n_blocks):
            s = rng.randrange(0, last+1); out.extend(all_rs[s:s+BLOCK])
        samples.append(mean(out[:n]))
    samples.sort()
    return sum(1 for s in samples if s <= 0)/N_RESAMPLES


def _evaluate(factory, params, primary):
    strat = factory(params)
    fold_rs = []
    for lo, hi in SPLITS:
        fold_rs.append(_trades(strat, _slice(primary, lo, hi)))
    return fold_rs


def audit_family(family: str, tf: str, primary, macro_frame, rng, sample_n: int):
    print(f"\n{'='*72}\n{family}  (tf={tf})\n{'='*72}")
    factory = _make_factory(family, macro_frame)
    if family == "dxy_lead_lag":
        grid = list(default_dxy_lead_lag_grid())
    elif family == "real_yield_reversal":
        grid = list(default_real_yield_reversal_grid())
    elif family == "macro_regime_continuation":
        grid = _macro_regime_grid()
    else:
        grid = _timed_horizon_grid()
    if len(grid) <= sample_n:
        sample = grid
    else:
        idx = rng.sample(range(len(grid)), sample_n)
        sample = [grid[i] for i in idx]
    print(f"grid={len(grid)} sampled={len(sample)}")

    cands = []
    for params in sample:
        try:
            fold_rs = _evaluate(factory, params, primary)
        except Exception as e:
            print(f"  param failed: {type(e).__name__}: {e}")
            continue
        train_rs = [r for i in TRAIN_FOLDS for r in fold_rs[i]]
        if len(train_rs) < MIN_TRAIN_TRADES:
            continue
        cands.append({
            "params": params, "fold_rs": fold_rs, "train_rs": train_rs,
            "holdout_rs": fold_rs[HOLDOUT_FOLD],
            "train_avgR": sum(train_rs)/len(train_rs),
        })
    if not cands:
        print("  no candidate met MIN_TRAIN_TRADES — REJECT-SPARSE")
        return
    cands.sort(key=lambda c: c["train_avgR"], reverse=True)
    best = cands[0]
    holdout = best["holdout_rs"]
    h_n = len(holdout)
    h_avg = mean(holdout) if h_n else 0.0
    full = best["train_rs"] + holdout
    p = _bootstrap_p(full, rng)
    p_bonf = min(1.0, p * len(sample)) if not math.isnan(p) else float("nan")

    print(f"\nBest by train(Y1-Y4) avgR:")
    if hasattr(best["params"], "__dict__"):
        print(f"  params: {asdict(best['params'])}")
    else:
        print(f"  params: {best['params']}")
    print(f"  train: n={len(best['train_rs'])}  avgR={best['train_avgR']:+.4f}  totalR={sum(best['train_rs']):+.2f}")
    for i in TRAIN_FOLDS:
        n, r, pf = _fold_metrics(best["fold_rs"][i])
        print(f"    Y{i+1}: n={n:3d}  pnl_r={r:+.2f}  PF={pf:.2f}")
    n_, r_, pf_ = _fold_metrics(holdout)
    print(f"  HOLDOUT (Y5): n={n_}  avgR={h_avg:+.4f}  totalR={r_:+.2f}  PF={pf_:.2f}")
    holdout_pass = (h_n >= 5 and h_avg > 0)
    print(f"  HOLDOUT VERDICT: {'PASS' if holdout_pass else 'FAIL'}")
    print(f"  full-5y bootstrap p(avgR<=0) = {p:.4f}  Bonferroni p = {p_bonf:.4f}")
    if not math.isnan(p_bonf) and p_bonf <= 0.05 and holdout_pass:
        verdict = "PREMIUM (post-Bonferroni + holdout PASS)"
    elif not math.isnan(p) and p <= 0.05 and holdout_pass:
        verdict = "PROVISIONAL (raw p<0.05 + holdout PASS)"
    elif holdout_pass:
        verdict = "WEAK (holdout PASS only)"
    else:
        verdict = "REJECT"
    print(f"  FINAL: {verdict}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tf", default="60m")
    parser.add_argument("--sample", type=int, default=80)
    parser.add_argument("--families", default="dxy_lead_lag,real_yield_reversal,macro_regime_continuation,timed_horizon_macro_regime")
    args = parser.parse_args()
    rng = random.Random(RNG_SEED)

    primary = load_bars_from_csv(Path("data/xauusd_5y") / f"xauusd_5y_{args.tf}.csv")
    print(f"primary={args.tf}  bars={len(primary):,}")
    macro_frame = load_macro_frame(Path("data/macro"))
    print(f"macro series available: {sorted(macro_frame.names())}")

    for fam in [f.strip() for f in args.families.split(",") if f.strip()]:
        try:
            audit_family(fam, args.tf, primary, macro_frame, rng, args.sample)
        except Exception as e:
            print(f"\n=== {fam} FAILED: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
