"""Macro-regime conditional analysis of asian_range_breakout (ARB).

For the 15-month dataset, runs ARB at the holdout-eval best params, takes
every closed trade, and groups them by:

* DXY 60-day trend at trade entry  (up / flat / down)
* VIX bucket at trade entry        (low <15 / mid 15-25 / high >25)
* DFII10 (10y real-yield) regime   (rising / flat / falling vs 30d mean)

Reports per-regime profit factor, win rate, average R, and trade count.

Usage:
    PYTHONPATH=src python scripts/macro_arb_regimes.py
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gold_trader.backtest.engine import run_backtest, BacktestConfig
from gold_trader.data import load_bars_from_csv
from gold_trader.strategies import AsianRangeBreakoutStrategy

CSV = ROOT / "data" / "xauusd_full_15m.csv"
MACRO = ROOT / "data" / "macro"


def _load_macro(name: str) -> list[tuple[date, float]]:
    rows: list[tuple[date, float]] = []
    with (MACRO / name).open() as fh:
        rd = csv.DictReader(fh)
        for r in rd:
            rows.append((date.fromisoformat(r["date"]), float(r["value"])))
    rows.sort(key=lambda x: x[0])
    return rows


def _value_on_or_before(series: list[tuple[date, float]], d: date) -> float | None:
    """Linear scan; series is small (<1500 rows)."""
    last = None
    for dd, v in series:
        if dd > d:
            break
        last = v
    return last


def _value_n_back(series: list[tuple[date, float]], d: date, n: int) -> float | None:
    """Value n trading days before `d`."""
    cutoff_idx = None
    for i, (dd, _v) in enumerate(series):
        if dd > d:
            break
        cutoff_idx = i
    if cutoff_idx is None or cutoff_idx - n < 0:
        return None
    return series[cutoff_idx - n][1]


def _trailing_mean(series: list[tuple[date, float]], d: date, n: int) -> float | None:
    cutoff_idx = None
    for i, (dd, _v) in enumerate(series):
        if dd > d:
            break
        cutoff_idx = i
    if cutoff_idx is None or cutoff_idx - n < 0:
        return None
    window = series[cutoff_idx - n + 1: cutoff_idx + 1]
    return sum(v for _, v in window) / len(window)


def classify_dxy(dxy: list[tuple[date, float]], d: date) -> str:
    now = _value_on_or_before(dxy, d)
    prior = _value_n_back(dxy, d, 60)
    if now is None or prior is None:
        return "unknown"
    pct = (now - prior) / prior
    if pct > 0.01:
        return "up"
    if pct < -0.01:
        return "down"
    return "flat"


def classify_vix(vix: list[tuple[date, float]], d: date) -> str:
    v = _value_on_or_before(vix, d)
    if v is None:
        return "unknown"
    if v < 15:
        return "low"
    if v <= 25:
        return "mid"
    return "high"


def classify_real10y(real: list[tuple[date, float]], d: date) -> str:
    now = _value_on_or_before(real, d)
    mean30 = _trailing_mean(real, d, 30)
    if now is None or mean30 is None:
        return "unknown"
    diff = now - mean30
    if diff > 0.05:
        return "rising"
    if diff < -0.05:
        return "falling"
    return "flat"


def aggregate(trades: list[tuple[str, float, float]]) -> dict[str, Any]:
    if not trades:
        return {"n": 0, "pf": float("nan"), "win_rate": float("nan"),
                "avg_r": float("nan"), "total_pnl": 0.0}
    wins = sum(1 for _, _r, p in trades if p > 0)
    gross_win = sum(p for _, _r, p in trades if p > 0)
    gross_loss = -sum(p for _, _r, p in trades if p < 0)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    avg_r = sum(r for _, r, _p in trades) / len(trades)
    return {
        "n": len(trades),
        "pf": pf,
        "win_rate": wins / len(trades),
        "avg_r": avg_r,
        "total_pnl": sum(p for _, _r, p in trades),
    }


def main() -> int:
    print(f"loading {CSV} ...")
    bars = load_bars_from_csv(str(CSV))
    print(f"  {len(bars)} bars from {bars[0].timestamp} to {bars[-1].timestamp}")

    # Best params from holdout-eval on data/xauusd_full_15m.csv (15-month)
    strategy = AsianRangeBreakoutStrategy(
        atr_period=20,
        risk_reward=2.0,
        max_spread=1.25,
        min_breakout_atr=0.05,
        min_range_atr=0.20,
        min_asian_bars=3,
        min_atr_threshold=10.0,
    )

    print("running ARB backtest ...")
    result = run_backtest(bars, strategy, BacktestConfig())
    print(f"  {len(result.trades)} closed trades")
    if not result.trades:
        print("no trades — abort")
        return 1

    dxy = _load_macro("dxy.csv")
    vix = _load_macro("vix.csv")
    real = _load_macro("real10y.csv")

    by_dxy: dict[str, list] = defaultdict(list)
    by_vix: dict[str, list] = defaultdict(list)
    by_real: dict[str, list] = defaultdict(list)
    by_combo: dict[tuple[str, str, str], list] = defaultdict(list)

    for t in result.trades:
        d = t.entry_time.astimezone(timezone.utc).date()
        dx = classify_dxy(dxy, d)
        vx = classify_vix(vix, d)
        rl = classify_real10y(real, d)
        rec = (t.entry_time.isoformat(), t.pnl_r, t.pnl)
        by_dxy[dx].append(rec)
        by_vix[vx].append(rec)
        by_real[rl].append(rec)
        by_combo[(dx, vx, rl)].append(rec)

    overall = aggregate([(t.entry_time.isoformat(), t.pnl_r, t.pnl)
                         for t in result.trades])

    out_dir = ROOT / "reports" / "macro_arb_regimes"
    out_dir.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("# Macro-regime conditional ARB analysis")
    lines.append("")
    lines.append(f"Source: {CSV.relative_to(ROOT)}")
    lines.append(f"Bars: {len(bars)}  trades: {len(result.trades)}")
    lines.append("")
    lines.append("## Overall (baseline)")
    lines.append("")
    lines.append(f"- n: {overall['n']}")
    lines.append(f"- profit_factor: {overall['pf']:.3f}")
    lines.append(f"- win_rate: {overall['win_rate']:.2%}")
    lines.append(f"- avg_r: {overall['avg_r']:+.3f}")
    lines.append(f"- total_pnl: ${overall['total_pnl']:+,.0f}")
    lines.append("")

    def emit(title: str, table: dict[str, list]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| regime | n | PF | win | avg_R | pnl |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for k in sorted(table.keys()):
            a = aggregate(table[k])
            pf_str = f"{a['pf']:.3f}" if a["pf"] != float("inf") else "inf"
            lines.append(
                f"| {k} | {a['n']} | {pf_str} | {a['win_rate']:.0%} | "
                f"{a['avg_r']:+.3f} | ${a['total_pnl']:+,.0f} |"
            )
        lines.append("")

    emit("By DXY 60d trend", by_dxy)
    emit("By VIX bucket", by_vix)
    emit("By 10y-real-yield regime", by_real)

    lines.append("## Top combined regimes (n >= 8)")
    lines.append("")
    lines.append("| dxy | vix | real | n | PF | win | avg_R |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: |")
    combos = []
    for k, v in by_combo.items():
        if len(v) >= 8:
            a = aggregate(v)
            combos.append((k, a))
    combos.sort(key=lambda kv: -kv[1]["pf"] if kv[1]["pf"] != float("inf") else -9e9)
    for (dx, vx, rl), a in combos:
        pf_str = f"{a['pf']:.3f}" if a["pf"] != float("inf") else "inf"
        lines.append(
            f"| {dx} | {vx} | {rl} | {a['n']} | {pf_str} | "
            f"{a['win_rate']:.0%} | {a['avg_r']:+.3f} |"
        )

    out_path = out_dir / "report.md"
    out_path.write_text("\n".join(lines) + "\n")
    print(f"wrote {out_path}")
    print("\n--- summary ---")
    print(f"baseline PF={overall['pf']:.3f}  n={overall['n']}")
    for label, table in [("DXY", by_dxy), ("VIX", by_vix), ("REAL", by_real)]:
        print(f"  {label}:")
        for k in sorted(table.keys()):
            a = aggregate(table[k])
            pf_str = f"{a['pf']:.3f}" if a["pf"] != float("inf") else "inf"
            print(f"    {k:10s} n={a['n']:4d} PF={pf_str} winR={a['win_rate']:.0%} avgR={a['avg_r']:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
