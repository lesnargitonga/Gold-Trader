"""Phase 10c: combine per-fold p-values via Fisher's method.

Per-fold permutation tests in scripts/mtf_phase10_robustness.py
returned p-values 0.12-0.39 — individually inconclusive at n≈50 each
but jointly informative. Fisher's combined p:

    chi2 = -2 * sum(ln(p_i))    df = 2k

is the canonical aggregator when the k folds are independent
(non-overlapping windows here are independent by construction).

We also run a tighter permutation per-fold at n=10000 to nail the
p-values precisely before combining.
"""
from __future__ import annotations

import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import build_indicator_caches, run_mtf_backtest, summarize_backtest
from gold_trader.data import build_mtf_bundle
from gold_trader.models import BacktestConfig
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


SPLITS = [
    ("Y1", datetime(2021, 5, 4, tzinfo=timezone.utc), datetime(2022, 5, 4, tzinfo=timezone.utc)),
    ("Y2", datetime(2022, 5, 4, tzinfo=timezone.utc), datetime(2023, 5, 4, tzinfo=timezone.utc)),
    ("Y3", datetime(2023, 5, 4, tzinfo=timezone.utc), datetime(2024, 5, 4, tzinfo=timezone.utc)),
    ("Y4", datetime(2024, 5, 4, tzinfo=timezone.utc), datetime(2025, 5, 4, tzinfo=timezone.utc)),
    ("Y5", datetime(2025, 5, 4, tzinfo=timezone.utc), datetime(2026, 5, 4, tzinfo=timezone.utc)),
]


def _sign_perm(trades, n_perm=10000, seed=42):
    if not trades:
        return 1.0, 0.0, 0
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
    return count / n_perm, obs_pf, len(trades)


def _chi2_sf(x, df):
    """Survival function (1 - CDF) for chi^2 — small-df closed form via
    incomplete gamma. Pure stdlib via math.lgamma + series."""
    # Use math.gamma regularized incomplete gamma upper Q(df/2, x/2).
    # Implement series for lower P, return 1 - P.
    a = df / 2.0
    z = x / 2.0
    if z <= 0:
        return 1.0
    # Lower regularized incomplete gamma series (good for z < a+1)
    if z < a + 1.0:
        term = 1.0 / a
        s = term
        n = 1
        while n < 1000:
            term *= z / (a + n)
            s += term
            if abs(term) < 1e-15 * abs(s):
                break
            n += 1
        p = s * math.exp(-z + a * math.log(z) - math.lgamma(a))
        return max(0.0, 1.0 - p)
    # Continued fraction for upper Q (good for z >= a+1)
    b = z + 1.0 - a
    c = 1.0 / 1e-300
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < 1e-300:
            d = 1e-300
        c = b + an / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    q = h * math.exp(-z + a * math.log(z) - math.lgamma(a))
    return max(0.0, min(1.0, q))


def fisher_combined(pvals):
    pvals = [max(p, 1e-9) for p in pvals]  # avoid log(0)
    chi2 = -2.0 * sum(math.log(p) for p in pvals)
    df = 2 * len(pvals)
    return chi2, df, _chi2_sf(chi2, df)


def main():
    primary, htf_bundle = load_5y_ladder(
        primary_tf="60m", htf_tfs=["240m", "1440m"],
        time_lo=datetime(2021, 4, 1, tzinfo=timezone.utc),
        time_hi=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )

    configs = [
        ("rl18_rr2.5", HTFBreakoutContinuation(align_tf="240m", range_lookback=18, risk_reward=2.5)),
        ("rl18_rr3.0", HTFBreakoutContinuation(align_tf="240m", range_lookback=18, risk_reward=3.0)),
        ("rl24_rr3.0", HTFBreakoutContinuation(align_tf="240m", range_lookback=24, risk_reward=3.0)),
    ]

    for cfg_label, inner in configs:
        gated = RegimeGatedMTF(inner=inner, align_tf="240m",
                                min_trend_strength_atr=0.5,
                                atr_pct_window=100, atr_pct_low=0.20, atr_pct_high=0.90)
        print(f"\n=== {cfg_label} ===")
        print(f"  fold   n     PF    avg_R    p-val (n=10000)")
        pvals = []
        for label, lo, hi in SPLITS:
            p_slice, h_slice = slice_window(primary, htf_bundle, lo, hi)
            bundle = build_mtf_bundle("60m", p_slice, h_slice)
            indicators = build_indicator_caches(bundle)
            res = run_mtf_backtest(bundle, gated, CONFIG, indicators=indicators)
            summary = summarize_backtest(res)
            p, pf, n = _sign_perm(res.trades, n_perm=10000)
            pvals.append(p)
            print(f"  {label}    {n:<4}  {pf:5.2f}  {summary.average_r:+.3f}  {p:.4f}")
        chi2, df, p_combined = fisher_combined(pvals)
        print(f"  Fisher combined:  chi2={chi2:.2f}  df={df}  p={p_combined:.4f}")
        if p_combined < 0.01:
            verdict = "STRONG SIGNAL (combined p<0.01)"
        elif p_combined < 0.05:
            verdict = "SIGNAL (combined p<0.05)"
        elif p_combined < 0.10:
            verdict = "WEAK SIGNAL (combined p<0.10)"
        else:
            verdict = "NOISE (combined p>=0.10)"
        print(f"  Verdict: {verdict}")


if __name__ == "__main__":
    main()
