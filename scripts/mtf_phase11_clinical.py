"""Phase 11: gate-overfit check + bootstrap CI + equity curve.

Three clinical tests on the rl=18 RR=2.5 + RegimeGate construct:

11a. GATE OVERFIT — sweep gate (ts, atr_pct_low, atr_pct_high) across
     a small grid on Y1-Y4 only; pick the train-best by total avgR;
     report Y5 OOS performance for that pick. If the train-best
     gate also wins Y5, the gate parameters aren't curve-fit.

11b. BLOCK BOOTSTRAP CI — concatenate trades across all 5 folds at
     fixed gate ts0.5/atr20-90; resample with block size 10 trades
     (preserves serial correlation); report 95% CI for avg_R.

11c. EQUITY CURVE — full 5y end-to-end run, compute max drawdown,
     longest losing streak, monthly netR. Quantifies path risk.
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

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


def _run(strategy, primary, htf_bundle, lo, hi):
    p_slice, h_slice = slice_window(primary, htf_bundle, lo, hi)
    bundle = build_mtf_bundle("60m", p_slice, h_slice)
    indicators = build_indicator_caches(bundle)
    res = run_mtf_backtest(bundle, strategy, CONFIG, indicators=indicators)
    return res, summarize_backtest(res)


def _make_gated(rl, rr, ts, lo, hi):
    inner = HTFBreakoutContinuation(align_tf="240m", range_lookback=rl, risk_reward=rr)
    return RegimeGatedMTF(inner=inner, align_tf="240m",
                          min_trend_strength_atr=ts,
                          atr_pct_window=100, atr_pct_low=lo, atr_pct_high=hi)


# ----------------------------------------------------------------------------
# 11a: gate overfit check
# ----------------------------------------------------------------------------

def gate_overfit(primary, htf_bundle):
    print("\n" + "=" * 80)
    print("11a — GATE OVERFIT CHECK")
    print("Train: Y1-Y4 (4y).  Hold-out: Y5.  Inner fixed: rl=18 RR=2.5")
    print("=" * 80)
    grid = [
        (ts, lo, hi)
        for ts in (0.3, 0.5, 0.7)
        for lo in (0.10, 0.20, 0.30)
        for hi in (0.85, 0.90, 0.95)
    ]

    train_folds = SPLITS[:4]
    test_fold = SPLITS[4]

    rows: list[tuple[tuple, float, float, int, float, float, int]] = []
    for ts, lo, hi in grid:
        gated = _make_gated(rl=18, rr=2.5, ts=ts, lo=lo, hi=hi)
        # Train aggregate
        train_avg_r_sum = 0.0
        train_pf_w = train_pf_l = 0.0
        train_n = 0
        train_pfs = []
        for label, t_lo, t_hi in train_folds:
            res, summary = _run(gated, primary, htf_bundle, t_lo, t_hi)
            train_n += len(res.trades)
            train_avg_r_sum += sum(t.pnl_r for t in res.trades)
            for t in res.trades:
                if t.pnl > 0:
                    train_pf_w += t.pnl
                else:
                    train_pf_l += -t.pnl
            train_pfs.append(summary.profit_factor if summary.profit_factor != float('inf') else 99.0)
        if train_n < 80:
            continue
        train_avg_r = train_avg_r_sum / train_n
        train_pf = train_pf_w / train_pf_l if train_pf_l > 0 else 999.0
        # Test
        res_t, summary_t = _run(gated, primary, htf_bundle, test_fold[1], test_fold[2])
        test_n = len(res_t.trades)
        if test_n == 0:
            continue
        test_avg_r = sum(t.pnl_r for t in res_t.trades) / test_n
        test_pf = summary_t.profit_factor if summary_t.profit_factor != float('inf') else 99.0
        rows.append(((ts, lo, hi), train_avg_r, train_pf, train_n, test_avg_r, test_pf, test_n))

    rows.sort(key=lambda r: r[1], reverse=True)
    print(f"\n  ts/lo/hi    train_n  train_PF  train_avgR | test_n  test_PF  test_avgR")
    print(f"  ----------  -------  --------  ---------- | ------  -------  ---------")
    for params, tar, tpf, tn, ear, epf, en in rows[:10]:
        print(f"  {params}    {tn:<7} {tpf:5.2f}     {tar:+.3f}      | {en:<6} {epf:5.2f}    {ear:+.3f}")

    print(f"\n  Top-3 by train_avg_R, all 9 train cells:")
    train_corr_x = [r[1] for r in rows]
    train_corr_y = [r[4] for r in rows]
    if len(train_corr_x) >= 3:
        # Spearman-ish via simple Pearson on ranks
        def rank(xs):
            sorted_xs = sorted(enumerate(xs), key=lambda p: p[1])
            ranks = [0] * len(xs)
            for i, (orig, _) in enumerate(sorted_xs):
                ranks[orig] = i
            return ranks
        rx, ry = rank(train_corr_x), rank(train_corr_y)
        n = len(rx)
        mx, my = mean(rx), mean(ry)
        num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
        dx = sum((r - mx) ** 2 for r in rx) ** 0.5
        dy = sum((r - my) ** 2 for r in ry) ** 0.5
        rho = num / (dx * dy) if dx > 0 and dy > 0 else float('nan')
        print(f"  Spearman rank correlation (train_avgR vs test_avgR) across {n} cells: rho={rho:+.3f}")
        if rho > 0.3:
            print("  -> Train ranking carries forward to test: gate IS NOT curve-fit.")
        elif rho < -0.1:
            print("  -> Train winners become test losers: gate IS curve-fit.")
        else:
            print("  -> Gate is essentially noise — train ranking gives no signal about test.")


# ----------------------------------------------------------------------------
# 11b: block bootstrap CI
# ----------------------------------------------------------------------------

def bootstrap_ci(primary, htf_bundle):
    print("\n" + "=" * 80)
    print("11b — BLOCK BOOTSTRAP CI (rl=18 RR=2.5, gate ts0.5/atr20-90)")
    print("Block size 10 trades, n_resamples=5000")
    print("=" * 80)
    gated = _make_gated(rl=18, rr=2.5, ts=0.5, lo=0.20, hi=0.90)
    all_rs: list[float] = []
    for label, lo, hi in SPLITS:
        res, _ = _run(gated, primary, htf_bundle, lo, hi)
        all_rs.extend(t.pnl_r for t in res.trades)
    if len(all_rs) < 30:
        print(f"  Insufficient trades: n={len(all_rs)}")
        return
    print(f"  Total trades pooled across 5y: n={len(all_rs)}")
    obs_avg = mean(all_rs)
    print(f"  Observed avg_R: {obs_avg:+.4f}")

    rng = random.Random(42)
    block = 10
    n_blocks = (len(all_rs) + block - 1) // block
    n_resamples = 5000
    samples = []
    for _ in range(n_resamples):
        out: list[float] = []
        for _ in range(n_blocks):
            start = rng.randrange(0, len(all_rs) - block + 1)
            out.extend(all_rs[start:start + block])
        out = out[:len(all_rs)]
        samples.append(mean(out))
    samples.sort()
    lo_ci = samples[int(0.025 * n_resamples)]
    hi_ci = samples[int(0.975 * n_resamples)]
    p_zero = sum(1 for s in samples if s <= 0) / n_resamples
    print(f"  95% block-bootstrap CI for avg_R: [{lo_ci:+.4f}, {hi_ci:+.4f}]")
    print(f"  P(avg_R <= 0) under bootstrap: {p_zero:.4f}")
    if lo_ci > 0:
        print("  -> Lower CI > 0: aggregate edge is significant at 95%.")
    elif p_zero < 0.10:
        print("  -> Lower CI <= 0 but P(<=0) < 0.10: weakly significant.")
    else:
        print("  -> CI overlaps zero comfortably: aggregate edge not significant.")


# ----------------------------------------------------------------------------
# 11c: equity curve / drawdown
# ----------------------------------------------------------------------------

def equity_curve(primary, htf_bundle):
    print("\n" + "=" * 80)
    print("11c — EQUITY CURVE (rl=18 RR=2.5, gate ts0.5/atr20-90)")
    print("=" * 80)
    gated = _make_gated(rl=18, rr=2.5, ts=0.5, lo=0.20, hi=0.90)
    trades = []
    for label, lo, hi in SPLITS:
        res, _ = _run(gated, primary, htf_bundle, lo, hi)
        trades.extend(res.trades)
    trades.sort(key=lambda t: t.entry_time)
    if not trades:
        print("  no trades"); return
    equity = [0.0]
    rs: list[float] = []
    for t in trades:
        r = t.pnl_r
        rs.append(r)
        equity.append(equity[-1] + r)
    peak = 0.0
    max_dd = 0.0
    max_dd_at = 0
    for i, e in enumerate(equity):
        if e > peak: peak = e
        dd = e - peak
        if dd < max_dd:
            max_dd = dd; max_dd_at = i
    # longest losing streak
    streak = max_streak = 0
    for r in rs:
        if r <= 0:
            streak += 1; max_streak = max(max_streak, streak)
        else:
            streak = 0
    wins = sum(1 for r in rs if r > 0)
    print(f"  total trades:         {len(trades)}")
    print(f"  win rate:             {wins/len(rs)*100:.1f}%")
    print(f"  total R:              {equity[-1]:+.2f}")
    print(f"  avg R / trade:        {mean(rs):+.4f}")
    print(f"  max drawdown (R):     {max_dd:.2f}  (after trade #{max_dd_at})")
    print(f"  longest losing streak: {max_streak} trades")
    print(f"  expected annual R:    {equity[-1]/5:.2f} (over 5y)")
    print(f"  expected annual %:    {equity[-1]/5 * 1.0:.2f}% (at 1% risk/trade)")
    # Year-by-year
    by_year = {}
    for t in trades:
        y = t.entry_time.year
        by_year.setdefault(y, []).append(t.pnl_r)
    print(f"\n  year   n     totalR    avgR    win%")
    for y in sorted(by_year):
        rs_y = by_year[y]
        wn = sum(1 for r in rs_y if r > 0)
        print(f"  {y}  {len(rs_y):<4}  {sum(rs_y):+.2f}    {mean(rs_y):+.3f}   {wn/len(rs_y)*100:.1f}%")


def main():
    primary, htf_bundle = load_5y_ladder(
        primary_tf="60m", htf_tfs=["240m", "1440m"],
        time_lo=datetime(2021, 4, 1, tzinfo=timezone.utc),
        time_hi=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    gate_overfit(primary, htf_bundle)
    bootstrap_ci(primary, htf_bundle)
    equity_curve(primary, htf_bundle)


if __name__ == "__main__":
    main()
