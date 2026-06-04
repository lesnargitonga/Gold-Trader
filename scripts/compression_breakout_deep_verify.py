#!/usr/bin/env python3
"""Deep verification of compression_breakout on 60m and 240m primary.

The premium_audit.py sweep flagged compression_breakout as PREMIUM on both
60m (p=0.0022) and 240m (p=0.0038) using SAMPLE_N=24 random params from a
14,580-item grid.  This script does the honest follow-up:

1. Sample SAMPLE_N=200 random params per TF.
2. Split: train = pick best by avgR on Y1-Y4 ONLY (require >=20 train trades).
3. Hold-out: report Y5 metrics for that param.
4. Aggregate 5y bootstrap p on the SELECTED param (still selection-biased,
   but bounded by SAMPLE_N=200).
5. Cross-TF replication check: best 60m param tested on 240m (and vice versa).

A real edge survives all four.  Selection-bias artifacts collapse on
hold-out or cross-TF.
"""
from __future__ import annotations

import math
import random
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import run_backtest
from gold_trader.data.csv_loader import load_bars_from_csv
from gold_trader.models import BacktestConfig, MarketBar
from gold_trader.research.family_grids import family_spec


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
TRAIN_FOLDS = [0, 1, 2, 3]   # Y1..Y4
HOLDOUT_FOLD = 4              # Y5

SAMPLE_N = 200
RNG_SEED = 31
BLOCK = 10
N_RESAMPLES = 5000
MIN_TRAIN_TRADES = 20
MIN_HOLDOUT_TRADES = 5


def _slice(bars: Sequence[MarketBar], lo: datetime, hi: datetime) -> list[MarketBar]:
    return [b for b in bars if lo <= b.timestamp <= hi]


def _trades(strategy, bars: Sequence[MarketBar]) -> list[float]:
    return [t.pnl_r for t in run_backtest(bars, strategy, CONFIG).trades]


def _fold_metrics(rs: list[float]) -> tuple[int, float, float]:
    n = len(rs)
    pnl_r = sum(rs)
    win = sum(r for r in rs if r > 0)
    loss = sum(-r for r in rs if r < 0)
    pf = win / loss if loss > 0 else (99.0 if win > 0 else 0.0)
    return n, pnl_r, pf


def _bootstrap_p(all_rs: list[float], rng: random.Random) -> float:
    if len(all_rs) < BLOCK * 2:
        return float("nan")
    n = len(all_rs)
    n_blocks = (n + BLOCK - 1) // BLOCK
    samples = []
    last_start = n - BLOCK
    for _ in range(N_RESAMPLES):
        out: list[float] = []
        for _ in range(n_blocks):
            s = rng.randrange(0, last_start + 1)
            out.extend(all_rs[s : s + BLOCK])
        samples.append(mean(out[:n]))
    samples.sort()
    return sum(1 for s in samples if s <= 0) / N_RESAMPLES


def _evaluate(family: str, params: Any, primary: list[MarketBar]):
    spec = family_spec(family)
    strategy = spec.factory(params)
    per_fold = []
    fold_rs: list[list[float]] = []
    for lo, hi in SPLITS:
        rs = _trades(strategy, _slice(primary, lo, hi))
        per_fold.append(_fold_metrics(rs))
        fold_rs.append(rs)
    return per_fold, fold_rs


def deep_audit(family: str, tf: str, rng: random.Random) -> dict:
    print(f"\n{'='*72}\nDEEP AUDIT  family={family}  primary_tf={tf}  sample={SAMPLE_N}\n{'='*72}")
    primary = load_bars_from_csv(Path("data/xauusd_5y") / f"xauusd_5y_{tf}.csv")
    spec = family_spec(family)
    grid = list(spec.grid)
    if len(grid) <= SAMPLE_N:
        sample = grid
    else:
        idx = rng.sample(range(len(grid)), SAMPLE_N)
        sample = [grid[i] for i in idx]
    print(f"primary_bars={len(primary):,}  grid_size={len(grid)}  sampled={len(sample)}")

    # 1) Run all sampled params, split metrics into train (Y1-Y4) and holdout (Y5).
    candidates = []
    for params in sample:
        per_fold, fold_rs = _evaluate(family, params, primary)
        train_rs = [r for i in TRAIN_FOLDS for r in fold_rs[i]]
        holdout_rs = fold_rs[HOLDOUT_FOLD]
        if len(train_rs) < MIN_TRAIN_TRADES:
            continue
        train_avgR = sum(train_rs) / len(train_rs)
        candidates.append({
            "params": params,
            "per_fold": per_fold,
            "fold_rs": fold_rs,
            "train_rs": train_rs,
            "holdout_rs": holdout_rs,
            "train_avgR": train_avgR,
        })
    if not candidates:
        print("  no candidate met MIN_TRAIN_TRADES")
        return {"family": family, "tf": tf, "verdict": "REJECT-SPARSE"}
    candidates.sort(key=lambda c: c["train_avgR"], reverse=True)

    # 2) Best by train avgR -> evaluate on holdout.
    best = candidates[0]
    holdout_rs = best["holdout_rs"]
    holdout_n = len(holdout_rs)
    holdout_avgR = mean(holdout_rs) if holdout_n else 0.0
    holdout_pf = (
        sum(r for r in holdout_rs if r > 0) /
        sum(-r for r in holdout_rs if r < 0)
    ) if any(r < 0 for r in holdout_rs) else float("inf")

    print(f"\nBest by train(Y1-Y4) avgR:")
    print(f"  params: {asdict(best['params'])}")
    print(f"  train: n={len(best['train_rs'])}  avgR={best['train_avgR']:+.4f}  totalR={sum(best['train_rs']):+.2f}")
    for i in TRAIN_FOLDS:
        n, r, pf = best["per_fold"][i]
        print(f"    Y{i+1}: n={n:3d}  pnl_r={r:+.2f}  PF={pf:.2f}")
    print(f"  HOLDOUT (Y5): n={holdout_n}  avgR={holdout_avgR:+.4f}  totalR={sum(holdout_rs):+.2f}  PF={holdout_pf:.2f}")
    holdout_passes = (holdout_n >= MIN_HOLDOUT_TRADES and holdout_avgR > 0)
    print(f"  HOLDOUT VERDICT: {'PASS' if holdout_passes else 'FAIL'}  (avgR>0 and n>={MIN_HOLDOUT_TRADES})")

    # 3) Bootstrap on full 5y for selected param (selection-biased, bounded by SAMPLE_N).
    all_rs = best["train_rs"] + best["holdout_rs"]
    p = _bootstrap_p(all_rs, rng)
    print(f"  full-5y bootstrap p(avgR<=0) = {p:.4f}  (selection-biased over {SAMPLE_N} candidates)")

    # 4) Bonferroni-corrected p (conservative).
    p_bonf = min(1.0, p * SAMPLE_N)
    print(f"  Bonferroni-corrected p = {p_bonf:.4f}")
    if p_bonf <= 0.05:
        verdict = "PREMIUM (post-Bonferroni)"
    elif p <= 0.05 and holdout_passes:
        verdict = "PROVISIONAL (raw p<0.05 + holdout PASS)"
    elif holdout_passes:
        verdict = "WEAK (holdout PASS only)"
    else:
        verdict = "REJECT"
    print(f"\n  FINAL VERDICT: {verdict}")
    return {
        "family": family, "tf": tf, "params": asdict(best["params"]),
        "train_avgR": best["train_avgR"],
        "holdout_n": holdout_n, "holdout_avgR": holdout_avgR, "holdout_pf": holdout_pf,
        "full5y_p": p, "p_bonf": p_bonf,
        "verdict": verdict,
    }


def cross_tf_check(family: str, params_a: dict, tf_a: str, tf_b: str, rng: random.Random) -> None:
    """Apply tf_a's selected params on tf_b primary; confirm cross-TF survival."""
    print(f"\n{'-'*72}\nCROSS-TF CHECK: {family} params from {tf_a} -> {tf_b}\n{'-'*72}")
    primary = load_bars_from_csv(Path("data/xauusd_5y") / f"xauusd_5y_{tf_b}.csv")
    spec = family_spec(family)
    # Reconstruct param dataclass.
    grid_cls = type(spec.grid[0])
    params = grid_cls(**params_a)
    per_fold, fold_rs = _evaluate(family, params, primary)
    all_rs = [r for rs in fold_rs for r in rs]
    n = len(all_rs)
    if n < 20:
        print(f"  cross-TF n={n} too small")
        return
    avgR = mean(all_rs)
    p = _bootstrap_p(all_rs, rng)
    print(f"  n={n}  avgR={avgR:+.4f}  totalR={sum(all_rs):+.2f}  p={p:.4f}")
    for i, (n_, r, pf) in enumerate(per_fold):
        print(f"    Y{i+1}: n={n_:3d}  pnl_r={r:+.2f}  PF={pf:.2f}")


def main() -> None:
    rng = random.Random(RNG_SEED)
    res60 = deep_audit("compression_breakout", "60m", rng)
    res240 = deep_audit("compression_breakout", "240m", rng)
    if "params" in res60:
        cross_tf_check("compression_breakout", res60["params"], "60m", "240m", rng)
    if "params" in res240:
        cross_tf_check("compression_breakout", res240["params"], "240m", "60m", rng)


if __name__ == "__main__":
    main()
