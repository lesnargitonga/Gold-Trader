"""Phase 10: harden the regime-gated 60m HTFBreakoutContinuation.

Runs two clinical checks back-to-back:

  10a — Per-fold sign-randomisation permutation test on the survivor
        (60m primary + 240m HTF + RegimeGate ts0.5/atr20-90, RR=3 rl=24).
        Confirms the per-fold PF lift over chop years isn't a coin-flip.

  10b — Inner-param sensitivity sweep under the *fixed* gate config.
        Sweeps range_lookback ∈ {12, 18, 24, 30} × RR ∈ {2.0, 2.5, 3.0}
        across all 5 folds. If Y1+Y2 PF≥1.0 holds across a contiguous
        block of the param grid, the regime-gate edge is structural,
        not a single happy point.
"""
from __future__ import annotations

import random
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import build_indicator_caches, run_mtf_backtest, summarize_backtest
from gold_trader.data import build_mtf_bundle
from gold_trader.models import BacktestConfig, MarketBar
from gold_trader.strategies.mtf_strategies import HTFBreakoutContinuation, RegimeGatedMTF
from gold_trader.validation import load_5y_ladder, slice_window


CONFIG = BacktestConfig(
    starting_equity=100_000.0,
    risk_fraction=0.01,
    max_hold_bars=24,
    kill_switch_drawdown_fraction=None,
    slippage_bps=2.0,
    commission_per_trade=1.0,
)


@dataclass(frozen=True)
class FoldSpec:
    label: str
    lo: datetime
    hi: datetime


SPLITS = [
    FoldSpec("Y1", datetime(2021, 5, 4, tzinfo=timezone.utc), datetime(2022, 5, 4, tzinfo=timezone.utc)),
    FoldSpec("Y2", datetime(2022, 5, 4, tzinfo=timezone.utc), datetime(2023, 5, 4, tzinfo=timezone.utc)),
    FoldSpec("Y3", datetime(2023, 5, 4, tzinfo=timezone.utc), datetime(2024, 5, 4, tzinfo=timezone.utc)),
    FoldSpec("Y4", datetime(2024, 5, 4, tzinfo=timezone.utc), datetime(2025, 5, 4, tzinfo=timezone.utc)),
    FoldSpec("Y5", datetime(2025, 5, 4, tzinfo=timezone.utc), datetime(2026, 5, 4, tzinfo=timezone.utc)),
]


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _run_fold(strategy, primary, htf_bundle, fold: FoldSpec):
    p_slice, h_slice = slice_window(primary, htf_bundle, fold.lo, fold.hi)
    if len(p_slice) < 200:
        return None
    bundle = build_mtf_bundle("60m", p_slice, h_slice)
    indicators = build_indicator_caches(bundle)
    res = run_mtf_backtest(bundle, strategy, CONFIG, indicators=indicators)
    summary = summarize_backtest(res)
    return res, summary


def _sign_perm_pvalue(trades, n_perm=2000, seed=42) -> tuple[float, float]:
    if not trades:
        return 1.0, 0.0
    pnls = [t.pnl for t in trades]
    magnitudes = [abs(p) for p in pnls]
    wins = sum(p for p in pnls if p > 0)
    losses = sum(-p for p in pnls if p < 0)
    obs_pf = wins / losses if losses > 0 else 999.0
    rng = random.Random(seed)
    count = 0
    for _ in range(n_perm):
        signs = [1 if rng.random() < 0.5 else -1 for _ in magnitudes]
        sim = [m * s for m, s in zip(magnitudes, signs)]
        w = sum(p for p in sim if p > 0)
        l = sum(-p for p in sim if p < 0)
        pf = w / l if l > 0 else 999.0
        if pf >= obs_pf:
            count += 1
    return count / n_perm, obs_pf


# ----------------------------------------------------------------------------
# 10a: per-fold permutation
# ----------------------------------------------------------------------------

def run_permutation_per_fold(primary, htf_bundle):
    print("\n" + "=" * 80)
    print("10a — PER-FOLD PERMUTATION TEST (sign-randomisation, n=2000)")
    print("Strategy: HTFBreakoutContinuation rl=24 RR=3 + RegimeGate ts0.5/atr20-90")
    print("=" * 80)
    inner = HTFBreakoutContinuation(align_tf="240m", range_lookback=24, risk_reward=3.0)
    gated = RegimeGatedMTF(inner=inner, align_tf="240m",
                           min_trend_strength_atr=0.5,
                           atr_pct_window=100, atr_pct_low=0.20, atr_pct_high=0.90)
    print(f"\n  fold  n      PF    avg_R    p-val   verdict")
    print(f"  ----  ----   ----   ------   ------  --------")
    for fold in SPLITS:
        out = _run_fold(gated, primary, htf_bundle, fold)
        if out is None:
            print(f"  {fold.label}    skipped (insufficient data)")
            continue
        res, summary = out
        p, pf = _sign_perm_pvalue(res.trades, n_perm=2000)
        verdict = ("SIGNAL" if p < 0.05 else
                   "WEAK" if p < 0.10 else
                   "MARGINAL" if p < 0.20 else
                   "NOISE")
        print(f"  {fold.label}    {len(res.trades):<4}   {pf:5.2f}  {summary.average_r:+.3f}   {p:.3f}   {verdict}")


# ----------------------------------------------------------------------------
# 10b: param sensitivity under fixed gate
# ----------------------------------------------------------------------------

def run_param_sensitivity(primary, htf_bundle):
    print("\n" + "=" * 80)
    print("10b — INNER-PARAM SENSITIVITY UNDER FIXED GATE (ts0.5/atr20-90)")
    print("Sweep: range_lookback × risk_reward → per-fold PF + folds≥1.0")
    print("=" * 80)
    rls = [12, 18, 24, 30]
    rrs = [2.0, 2.5, 3.0]
    header = f"  rl  RR  | " + "  ".join(f"{f.label:>5}" for f in SPLITS) + "  | folds≥1.0  total_n  median_PF"
    print(header)
    print("  " + "-" * (len(header) - 2))

    grid_results: list[tuple[int, float, list[float], int, int, float]] = []
    for rl in rls:
        for rr in rrs:
            inner = HTFBreakoutContinuation(align_tf="240m", range_lookback=rl, risk_reward=rr)
            gated = RegimeGatedMTF(inner=inner, align_tf="240m",
                                    min_trend_strength_atr=0.5,
                                    atr_pct_window=100, atr_pct_low=0.20, atr_pct_high=0.90)
            pfs: list[float] = []
            n_total = 0
            for fold in SPLITS:
                out = _run_fold(gated, primary, htf_bundle, fold)
                if out is None:
                    pfs.append(float("nan"))
                    continue
                res, summary = out
                pf = summary.profit_factor if summary.profit_factor != float("inf") else 99.0
                pfs.append(pf)
                n_total += len(res.trades)
            folds_ok = sum(1 for p in pfs if p == p and p >= 1.0)  # NaN-safe
            valid = [p for p in pfs if p == p]
            med = sorted(valid)[len(valid) // 2] if valid else float("nan")
            row = "  " + f"{rl:>2}  {rr:>3}  | " + "  ".join(f"{p:5.2f}" if p == p else "  -- " for p in pfs) + f"  |   {folds_ok}/5     {n_total:>5}    {med:.2f}"
            print(row)
            grid_results.append((rl, rr, pfs, folds_ok, n_total, med))

    # robust-cell summary
    print()
    robust = [r for r in grid_results if r[3] >= 4 and r[4] >= 100]
    print(f"  Cells with folds≥1.0 in 4/5 AND total_n≥100: {len(robust)}/{len(grid_results)}")
    for rl, rr, pfs, folds_ok, n_total, med in robust:
        print(f"    rl={rl} RR={rr}: folds_ok={folds_ok}/5 n={n_total} median_PF={med:.2f}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    primary, htf_bundle = load_5y_ladder(
        primary_tf="60m", htf_tfs=["240m", "1440m"],
        time_lo=datetime(2021, 4, 1, tzinfo=timezone.utc),
        time_hi=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    run_permutation_per_fold(primary, htf_bundle)
    run_param_sensitivity(primary, htf_bundle)


if __name__ == "__main__":
    main()
