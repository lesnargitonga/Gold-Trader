#!/usr/bin/env python3
"""Premium audit — single-TF families under Phase-13 honesty bar.

Runs every self-contained strategy family on the canonical 5y dataset at
retail costs ($1/trade + 2 bps slippage), splits into 5 × 1y folds, and
applies the same statistical bar that killed the MTF construct:

    one-sided block-bootstrap P(avgR <= 0)  with block_size=10, n_resamples=5000

For each family this prints per-fold (n, pnl_r, PF) and an aggregate verdict.
Premium tier = bootstrap p <= 0.05; Probationary = p <= 0.10; Reject = p > 0.10.

For each family we use the first grid entry as the baseline param set, then
also evaluate up to TOP_K params chosen by raw 5y avg_R among a SAMPLE_N
random subsample of the grid (no fold separation — this is candidate
generation, the bootstrap on the chosen winner is the honest measure since
selection bias is bounded by SAMPLE_N).
"""
from __future__ import annotations

import argparse
import math
import random
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import run_backtest  # noqa: E402
from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.models import BacktestConfig, MarketBar  # noqa: E402
from gold_trader.research.family_grids import (  # noqa: E402
    all_self_contained_families,
    family_spec,
)


CONFIG = BacktestConfig(
    starting_equity=100_000.0,
    risk_fraction=0.01,
    max_hold_bars=24,
    kill_switch_drawdown_fraction=None,
    slippage_bps=2.0,
    commission_per_trade=1.0,
)

PRIMARY_TF = "15m"
DATA_FILE = Path("data/xauusd_5y") / f"xauusd_5y_{PRIMARY_TF}.csv"

# 5 × 1y folds (Y1..Y5)
SPLITS = [
    (
        datetime(2021 + i, 5, 4, tzinfo=timezone.utc),
        datetime(2022 + i, 5, 4, tzinfo=timezone.utc),
    )
    for i in range(5)
]

# Param search budget: pick TOP_K by raw 5y avg_R from SAMPLE_N random params.
SAMPLE_N = 24
TOP_K = 3
RNG_SEED = 17

# Bootstrap settings (must match Phase 11/12/13).
BLOCK = 10
N_RESAMPLES = 5000


def _load_primary(tf: str) -> list[MarketBar]:
    path = Path("data/xauusd_5y") / f"xauusd_5y_{tf}.csv"
    if not path.exists():
        raise SystemExit(f"missing dataset: {path}")
    return load_bars_from_csv(path)


def _slice(bars: Sequence[MarketBar], lo: datetime, hi: datetime) -> list[MarketBar]:
    return [b for b in bars if lo <= b.timestamp <= hi]


def _backtest_trades(strategy, bars: Sequence[MarketBar]) -> list[float]:
    res = run_backtest(bars, strategy, CONFIG)
    return [t.pnl_r for t in res.trades]


def _fold_metrics(rs: list[float]) -> tuple[int, float, float]:
    n = len(rs)
    pnl_r = sum(rs)
    win = sum(r for r in rs if r > 0)
    loss = sum(-r for r in rs if r < 0)
    pf = win / loss if loss > 0 else (99.0 if win > 0 else 0.0)
    return n, pnl_r, pf


def _bootstrap_p_avgR_le_zero(all_rs: list[float], rng: random.Random) -> float:
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
        out = out[:n]
        samples.append(mean(out))
    samples.sort()
    return sum(1 for s in samples if s <= 0) / N_RESAMPLES


def _evaluate_param_full5y(family: str, params: Any, primary: list[MarketBar]):
    """Run full 5y once. Returns list[r] across all folds + per-fold metrics."""
    spec = family_spec(family)
    strategy = spec.factory(params)
    per_fold = []
    all_rs: list[float] = []
    for lo, hi in SPLITS:
        bars = _slice(primary, lo, hi)
        rs = _backtest_trades(strategy, bars)
        per_fold.append(_fold_metrics(rs))
        all_rs.extend(rs)
    return all_rs, per_fold


def _verdict(p: float) -> str:
    if math.isnan(p):
        return "INSUFFICIENT-N"
    if p <= 0.05:
        return "PREMIUM"
    if p <= 0.10:
        return "PROBATIONARY"
    return "REJECT"


def audit_family(family: str, primary: list[MarketBar], rng: random.Random) -> dict:
    spec = family_spec(family)
    grid = list(spec.grid)
    # Baseline = first grid entry.
    baseline = grid[0]

    # Candidate sample: SAMPLE_N random distinct from grid (always include baseline).
    if len(grid) <= SAMPLE_N:
        sample = grid
    else:
        sample_idx = rng.sample(range(1, len(grid)), SAMPLE_N - 1)
        sample = [baseline] + [grid[i] for i in sample_idx]

    print(f"\n=== {family}  (grid={len(grid)}, sampling {len(sample)}) ===")
    candidate_results = []
    for params in sample:
        all_rs, _ = _evaluate_param_full5y(family, params, primary)
        n = len(all_rs)
        avgR = (sum(all_rs) / n) if n else 0.0
        candidate_results.append((avgR, n, params, all_rs))
    # Filter: require >= 25 trades over 5y (5/yr) to avoid degenerate winners.
    eligible = [c for c in candidate_results if c[1] >= 25]
    if not eligible:
        print(f"  no param produced >=25 trades — REJECT (sparse)")
        return {"family": family, "verdict": "REJECT-SPARSE"}
    eligible.sort(key=lambda c: c[0], reverse=True)
    top = eligible[:TOP_K]

    family_summary = {"family": family, "candidates": []}
    for rank, (avgR, n, params, all_rs) in enumerate(top, 1):
        # Re-derive per-fold for display.
        _, per_fold = _evaluate_param_full5y(family, params, primary)
        p = _bootstrap_p_avgR_le_zero(all_rs, rng)
        verdict = _verdict(p)
        pf_str = " / ".join(f"{m[2]:.2f}" for m in per_fold)
        n_str = " / ".join(str(m[0]) for m in per_fold)
        total_r = sum(all_rs)
        print(
            f"  #{rank}  n={n:4d}  avgR={avgR:+.4f}  totalR={total_r:+7.2f}  "
            f"p={p:.4f}  [{verdict}]  PF/yr={pf_str}  n/yr={n_str}"
        )
        family_summary["candidates"].append(
            {
                "rank": rank,
                "params": asdict(params),
                "n": n,
                "avgR": avgR,
                "total_r": total_r,
                "p_avgR_le_zero": p,
                "verdict": verdict,
                "per_fold_pf": [m[2] for m in per_fold],
                "per_fold_n": [m[0] for m in per_fold],
                "per_fold_pnl_r": [m[1] for m in per_fold],
            }
        )
    return family_summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tf", default="15m", help="primary TF (15m, 60m, 240m)")
    parser.add_argument("--sample", type=int, default=24, help="random params sampled per family")
    parser.add_argument("--families", default="", help="comma list; empty = all")
    args = parser.parse_args()

    global SAMPLE_N
    SAMPLE_N = args.sample
    rng = random.Random(RNG_SEED)
    primary = _load_primary(args.tf)
    print(f"primary={args.tf} bars={len(primary):,}  sample={SAMPLE_N}  config={CONFIG}")
    print(f"folds: " + ", ".join(f"Y{i+1} {lo.date()}->{hi.date()}" for i, (lo, hi) in enumerate(SPLITS)))

    if args.families.strip():
        families = [f.strip() for f in args.families.split(",") if f.strip()]
    else:
        families = all_self_contained_families()
    summaries = []
    for fam in families:
        try:
            s = audit_family(fam, primary, rng)
            summaries.append(s)
        except Exception as exc:  # surface, don't abort sweep
            print(f"\n=== {fam} === FAILED: {type(exc).__name__}: {exc}")
            summaries.append({"family": fam, "verdict": f"ERROR:{exc}"})

    # Ranked summary.
    print("\n\n" + "=" * 72)
    print("RANKED SUMMARY (best candidate per family by bootstrap p)")
    print("=" * 72)
    ranked = []
    for s in summaries:
        cands = s.get("candidates", [])
        if not cands:
            ranked.append((float("inf"), s["family"], s.get("verdict", "?"), None))
            continue
        best = min(cands, key=lambda c: c["p_avgR_le_zero"] if not math.isnan(c["p_avgR_le_zero"]) else 1.0)
        ranked.append((best["p_avgR_le_zero"], s["family"], best["verdict"], best))
    ranked.sort(key=lambda r: (math.isnan(r[0]) if isinstance(r[0], float) else False, r[0]))
    for p, fam, verdict, best in ranked:
        if best is None:
            print(f"  {fam:32s}  {verdict}")
        else:
            print(
                f"  {fam:32s}  p={best['p_avgR_le_zero']:.4f}  "
                f"avgR={best['avgR']:+.4f}  n={best['n']:4d}  totalR={best['total_r']:+6.2f}  [{verdict}]"
            )

    premium = [r for r in ranked if r[2] == "PREMIUM"]
    probation = [r for r in ranked if r[2] == "PROBATIONARY"]
    print(f"\nPREMIUM ({len(premium)}): {[r[1] for r in premium]}")
    print(f"PROBATIONARY ({len(probation)}): {[r[1] for r in probation]}")


if __name__ == "__main__":
    main()
