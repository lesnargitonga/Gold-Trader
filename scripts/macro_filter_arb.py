"""ARB × macro-regime conditional analysis.

Question: does conditioning ARB entries on the macro regime discovered by
the pattern-mining sweep improve PF, win-rate, or expectancy?

Approach (pure post-hoc, no overfit risk in either direction):

1. Run ARB at canonical params on the full 15-month dataset.
2. For each completed trade, look up the macro regime *as of the entry
   bar*.
3. Bucket trades by regime and report PF / win-rate / avg_R for each
   bucket.
4. Combine the strongest single-leg filters into a compound and report
   the filtered PF vs full PF.

This is a *measurement*, not a strategy promotion.  If a regime filter
reliably removes ~30% of trades while doubling PF, that's evidence to
build a macro-gated ARB variant and run it through holdout-eval.

Run:

    PYTHONPATH=src .venv/bin/python scripts/macro_filter_arb.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest.engine import run_backtest  # noqa: E402
from gold_trader.data import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame, MacroFrame  # noqa: E402
from gold_trader.models import BacktestConfig  # noqa: E402
from gold_trader.strategies.asian_range_breakout import (  # noqa: E402
    AsianRangeBreakoutStrategy,
)


def _classify_trade(macro: MacroFrame, ts) -> dict[str, bool]:
    """Compute boolean macro filters as of trade entry."""
    out: dict[str, bool] = {}

    real = macro.get("real10y")
    dxy = macro.get("dxy")
    vix = macro.get("vix")
    fedfunds = macro.get("fedfunds")
    bei = macro.get("bei10")
    spx = macro.get("spx")

    # Real-yield direction (5d).
    if real is not None:
        d = real.change(ts, lookback_days=5)
        if d is not None:
            out["real10y_5d_down"] = d < 0
            out["real10y_5d_up"] = d > 0

    # DXY direction & consolidation.
    if dxy is not None:
        d5 = dxy.pct_change(ts, lookback_days=5)
        d20 = dxy.pct_change(ts, lookback_days=20)
        if d5 is not None:
            out["dxy_5d_down"] = d5 < 0
            out["dxy_5d_up"] = d5 > 0
        if d20 is not None:
            out["dxy_20d_flat"] = abs(d20) * 100.0 <= 1.0

    # VIX regime.
    if vix is not None:
        v = vix.as_of(ts)
        d5 = vix.change(ts, lookback_days=5)
        if v is not None:
            out["vix_low"] = v < 15
            out["vix_high"] = v > 25
        if d5 is not None:
            out["vix_5d_calm"] = abs(d5) <= 1.5

    # Fed funds direction (60d).
    if fedfunds is not None:
        d = fedfunds.change(ts, lookback_days=60)
        if d is not None:
            out["fedfunds_60d_down"] = d < 0

    # Inflation expectations.
    if bei is not None:
        d = bei.change(ts, lookback_days=5)
        if d is not None:
            out["bei10_5d_up"] = d > 0
            out["bei10_5d_down"] = d < 0

    # SPX direction.
    if spx is not None:
        d = spx.pct_change(ts, lookback_days=5)
        if d is not None:
            out["spx_5d_up"] = d > 0
            out["spx_5d_down"] = d < 0

    # Compound: gold tailwind = DXY↓ AND real↓.
    if real is not None and dxy is not None:
        dr = real.change(ts, lookback_days=5)
        dd = dxy.pct_change(ts, lookback_days=5)
        if dr is not None and dd is not None:
            out["gold_tailwind"] = dr < 0 and dd < 0

    # Stagflation block (gold-bearish per mining).
    if real is not None and bei is not None:
        rv = real.as_of(ts)
        bv = bei.as_of(ts)
        if rv is not None and bv is not None:
            real_vals = sorted(p.value for p in real.points)
            bei_vals = sorted(p.value for p in bei.points)
            if len(real_vals) >= 9 and len(bei_vals) >= 9:
                real_lo = real_vals[len(real_vals) // 3]
                bei_hi = bei_vals[(2 * len(bei_vals)) // 3]
                out["stagflation"] = bv >= bei_hi and rv <= real_lo

    return out


def _stats(trades) -> dict:
    if not trades:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "avg_r": 0.0, "total_r": 0.0}
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    gp = sum(t.pnl for t in wins)
    gl = abs(sum(t.pnl for t in losses))
    pf = (gp / gl) if gl > 0 else float("inf") if gp > 0 else 0.0
    return {
        "n": len(trades),
        "wr": len(wins) / len(trades),
        "pf": pf,
        "avg_r": sum(t.pnl_r for t in trades) / len(trades),
        "total_r": sum(t.pnl_r for t in trades),
    }


def _print_row(label: str, s: dict, baseline_n: int | None = None) -> None:
    pf_s = f"{s['pf']:>6.3f}" if s["pf"] != float("inf") else "    inf"
    extra = ""
    if baseline_n and s["n"]:
        extra = f"  ({100 * s['n'] / baseline_n:>5.1f}% kept)"
    print(
        f"  {label:<35} n={s['n']:>4d}  wr={s['wr']:>5.1%}  "
        f"pf={pf_s}  avg_r={s['avg_r']:>+6.3f}  "
        f"total_r={s['total_r']:>+7.2f}{extra}"
    )


def main() -> int:
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "data/xauusd_full_15m.csv"
    macro_dir = "data/macro"

    bars = load_bars_from_csv(csv_path)
    if not bars:
        print(f"no bars in {csv_path}", file=sys.stderr)
        return 2
    print(f"bars: {len(bars)} ({bars[0].timestamp} → {bars[-1].timestamp})")

    macro = load_macro_frame(macro_dir)
    print(f"macro frame: {sorted(macro.names())}")

    # Canonical post-fix ARB params (per memory & evaluation.md).
    strat = AsianRangeBreakoutStrategy(
        atr_period=10,
        risk_reward=2.5,
        max_spread=1.00,
        min_atr_threshold=0.0,
    )
    config = BacktestConfig(
        starting_equity=10_000.0,
        commission_per_trade=10.0,
    )
    print(f"running ARB ({strat}) ...")
    result = run_backtest(bars, strat, config)
    trades = list(result.trades)
    print(f"ARB closed trades: {len(trades)}")
    if not trades:
        print("no trades — abort")
        return 0

    base = _stats(trades)
    print()
    print("=== Baseline (all ARB trades) ===")
    _print_row("ALL", base)
    print()

    # Tag every trade with its macro regime at entry.
    tagged: list[tuple[dict[str, bool], object]] = []
    for t in trades:
        tags = _classify_trade(macro, t.entry_time)
        tagged.append((tags, t))

    # Single-filter analysis.
    print("=== Single macro-filter buckets (KEEP = filter True) ===")
    filter_keys = sorted({k for tags, _ in tagged for k in tags})
    rows = []
    for key in filter_keys:
        kept = [t for tags, t in tagged if tags.get(key) is True]
        rejected = [t for tags, t in tagged if tags.get(key) is False]
        s_kept = _stats(kept)
        s_rej = _stats(rejected)
        rows.append((key, s_kept, s_rej))

    # Sort by PF of kept side, descending; require enough trades.
    rows.sort(
        key=lambda r: (
            r[1]["pf"] if r[1]["n"] >= 20 else -1.0,
        ),
        reverse=True,
    )
    print("KEEP filter (trades where filter == True):")
    for key, s, _ in rows:
        _print_row(key, s, baseline_n=base["n"])
    print()
    print("REJECT filter (trades where filter == False):")
    rows_rej = sorted(
        rows,
        key=lambda r: r[2]["pf"] if r[2]["n"] >= 20 else -1.0,
        reverse=True,
    )
    for key, _, s in rows_rej:
        _print_row(key, s, baseline_n=base["n"])

    print()
    print("=== Compound filters (research-only, in-sample) ===")

    # Long trades only, filtered by gold-bullish regimes.
    longs = [(tags, t) for tags, t in tagged if str(t.side).endswith("LONG")]
    shorts = [(tags, t) for tags, t in tagged if str(t.side).endswith("SHORT")]
    print(f"  side split: longs={len(longs)}  shorts={len(shorts)}")

    def _compound(name: str, predicate) -> None:
        kept = [t for tags, t in tagged if predicate(tags)]
        s = _stats(kept)
        _print_row(name, s, baseline_n=base["n"])

    _compound(
        "real10y_5d_down",
        lambda tg: tg.get("real10y_5d_down") is True,
    )
    _compound(
        "dxy_20d_flat",
        lambda tg: tg.get("dxy_20d_flat") is True,
    )
    _compound(
        "vix_5d_calm",
        lambda tg: tg.get("vix_5d_calm") is True,
    )
    _compound(
        "gold_tailwind",
        lambda tg: tg.get("gold_tailwind") is True,
    )
    _compound(
        "real10y_5d_down & vix_5d_calm",
        lambda tg: tg.get("real10y_5d_down") and tg.get("vix_5d_calm"),
    )
    _compound(
        "dxy_20d_flat & vix_5d_calm",
        lambda tg: tg.get("dxy_20d_flat") and tg.get("vix_5d_calm"),
    )
    _compound(
        "real10y_5d_down OR dxy_20d_flat",
        lambda tg: tg.get("real10y_5d_down") or tg.get("dxy_20d_flat"),
    )
    _compound(
        "(any-bullish) AND NOT stagflation",
        lambda tg: (
            (
                tg.get("real10y_5d_down")
                or tg.get("dxy_20d_flat")
                or tg.get("gold_tailwind")
            )
            and not tg.get("stagflation")
        ),
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
