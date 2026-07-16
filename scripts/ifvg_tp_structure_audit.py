#!/usr/bin/env python3
"""TP-structure test for HTF IFVG — structural ("next IFVG/swing") vs fixed 5R.

Same detector / window / point-in-time HTF as scripts/ifvg_htf_edge_audit.py.
What changes here is ONLY the exit model:

  STOP   : just outside the originating IFVG — plan.stop (current invalidation
           logic = gap edge + stop_buffer_atr, max'd with the sweep level).
           Loss is therefore exactly -1R by construction.

  TARGET (two variants, run side by side):
    * structural : nearest opposing structure beyond entry in the trade
                   direction — nearest swing pivot (window=2), nearest 3-bar
                   FVG gap edge, or configured market level — whichever is
                   closest. "Capped at the structural level", i.e. the target
                   IS that level, never extended to a fixed multiple. This is
                   the "it reacts again on the next IFVG" thesis.
    * fixed_5r   : entry +/- 5 * risk. Direct comparison benchmark.

Resolution: scan forward up to a generous horizon. Within a bar, STOP is
checked before TARGET (conservative tie-break). Trades that reach neither in
the horizon are "open" and are EXCLUDED from hit-rate / R / PF (disclosed as
n_open) — "hit rate = % reaching target before stop" is over resolved trades.

Per slice (grade A / B / B-and-above  x  TF 4H / 1H / both):
  n, n_open, hit_rate, avg_r, total_r, profit_factor,
  avg_win_rr (realised reward:risk on winners),
  breakeven_wr = 1/(1+avg_win_rr),  clears = hit_rate > breakeven_wr.

Re-run:
  PYTHONPATH=src .venv/bin/python scripts/ifvg_tp_structure_audit.py \
      --start 2026-04-17 --end 2026-06-19
"""
from __future__ import annotations

import argparse
import json
import statistics as stats
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.assistants.ifvg_confluence import (  # noqa: E402
    IFVGAssistantConfig,
    find_ifvg_setups,
    load_market_levels,
    setup_to_dict,
)
from gold_trader.calendar import NewsCalendar  # noqa: E402
from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.research.realtime_research import OpenAIResearchConfig  # noqa: E402

LEVELS = REPO / "config" / "market_levels.json"
NEWS = REPO / "data" / "macro" / "news_calendar.csv"
# generous horizon so far structural / 5R targets get a fair chance (~20 trading days)
HORIZON = {60: 480, 240: 120, 15: 1920}
PIVOT_W = 2
STRUCT_LOOKBACK = 80


def _parse_day(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _is_pivot_high(bars, i, w):
    if i - w < 0 or i + w >= len(bars):
        return False
    hi = bars[i].high
    return all(j == i or bars[j].high < hi for j in range(i - w, i + w + 1))


def _is_pivot_low(bars, i, w):
    if i - w < 0 or i + w >= len(bars):
        return False
    lo = bars[i].low
    return all(j == i or bars[j].low > lo for j in range(i - w, i + w + 1))


def _structural_target(bars, signal_idx, side, entry, levels):
    """Nearest opposing structure beyond entry in the trade direction.

    Candidates: swing pivots (w=2), 3-bar FVG gap near-edges, market levels.
    Long  -> nearest level ABOVE entry. Short -> nearest level BELOW entry.
    """
    cands: list[float] = []
    start = max(PIVOT_W, signal_idx - STRUCT_LOOKBACK)
    # swing pivots
    for i in range(start, signal_idx - PIVOT_W):
        if side == "long" and _is_pivot_high(bars, i, PIVOT_W) and bars[i].high > entry:
            cands.append(bars[i].high)
        if side == "short" and _is_pivot_low(bars, i, PIVOT_W) and bars[i].low < entry:
            cands.append(bars[i].low)
    # 3-bar FVG gaps (near edge = first reaction point)
    for k in range(start + 2, signal_idx + 1):
        b2, bc = bars[k - 2], bars[k]
        if b2.high < bc.low:          # bullish gap [b2.high, bc.low]
            lo, hi = b2.high, bc.low
        elif b2.low > bc.high:        # bearish gap [bc.high, b2.low]
            lo, hi = bc.high, b2.low
        else:
            continue
        if side == "long" and lo > entry:
            cands.append(lo)
        if side == "short" and hi < entry:
            cands.append(hi)
    # configured market levels
    for lv in levels:
        if side == "long" and lv.price > entry:
            cands.append(lv.price)
        if side == "short" and lv.price < entry:
            cands.append(lv.price)
    if not cands:
        return None
    return min(cands) if side == "long" else max(cands)


def collect(bars, *, tf, start, end, macro, levels, calendar):
    cfg = IFVGAssistantConfig()
    warmup = max(cfg.atr_period + 5, 80)
    research_off = OpenAIResearchConfig(enabled=False, mode="off")
    seen: set[tuple] = set()
    out: list[dict] = []
    for i in range(warmup, len(bars)):
        ts = bars[i].timestamp
        if ts < start or ts > end:
            continue
        setups = find_ifvg_setups(
            bars, index=i, macro_frame=macro, market_levels=levels,
            news_calendar=calendar, higher_timeframe_bias=None,
            openai_research_config=research_off,
        )
        if not setups:
            continue
        best = setups[0]
        d = setup_to_dict(best, timeframe_minutes=tf)
        letter = str((d.get("grading") or {}).get("letter") or "D")
        if letter not in ("A", "B"):
            continue
        c = best.candidate
        key = (tf, c.side.value, c.formation_idx, c.inversion_idx, c.signal_idx)
        if key in seen:
            continue
        seen.add(key)
        plan = best.plan
        entry, stop = plan.entry, plan.stop
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        struct = _structural_target(bars, i, c.side.value, entry, levels)
        out.append({
            "tf": tf, "side": c.side.value, "grade": letter, "bar_idx": i,
            "entry": entry, "stop": stop, "risk": risk,
            "target_structural": struct,
            "target_5r": entry + 5 * risk if c.side.value == "long" else entry - 5 * risk,
        })
    return out


def _resolve(bars, idx, side, entry, stop, target, max_bars):
    """Return ('win'|'loss'|'open', r). Stop checked before target (conservative)."""
    if target is None:
        return "open", 0.0
    is_long = side == "long"
    if is_long and target <= entry:
        return "open", 0.0
    if not is_long and target >= entry:
        return "open", 0.0
    risk = abs(entry - stop)
    rr = abs(target - entry) / risk
    end = min(len(bars), idx + 1 + max_bars)
    for j in range(idx + 1, end):
        b = bars[j]
        if is_long:
            if b.low <= stop:
                return "loss", -1.0
            if b.high >= target:
                return "win", rr
        else:
            if b.high >= stop:
                return "loss", -1.0
            if b.low <= target:
                return "win", rr
    return "open", 0.0


def stat_block(results):
    """results: list of (outcome, r). Opens excluded from rates/R, disclosed."""
    resolved = [(o, r) for (o, r) in results if o in ("win", "loss")]
    opens = sum(1 for (o, _) in results if o == "open")
    n = len(resolved)
    if n == 0:
        return {"n": 0, "n_open": opens, "hit_rate": None, "avg_r": None,
                "total_r": 0.0, "profit_factor": None, "avg_win_rr": None,
                "breakeven_wr": None, "clears": None}
    wins = [r for (o, r) in resolved if o == "win"]
    losses = [r for (o, r) in resolved if o == "loss"]
    hit = len(wins) / n
    avg_win_rr = stats.mean(wins) if wins else None
    be = 1.0 / (1.0 + avg_win_rr) if avg_win_rr else None
    return {
        "n": n,
        "n_open": opens,
        "hit_rate": round(hit, 3),
        "avg_r": round(stats.mean(r for (_, r) in resolved), 3),
        "total_r": round(sum(r for (_, r) in resolved), 2),
        "profit_factor": (round(sum(wins) / abs(sum(losses)), 2)
                          if losses and sum(losses) != 0 else (None if wins else 0.0)),
        "avg_win_rr": round(avg_win_rr, 3) if avg_win_rr else None,
        "breakeven_wr": round(be, 3) if be else None,
        "clears": (hit > be) if be is not None else None,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2026-04-17")
    p.add_argument("--end", default="2026-06-19")
    p.add_argument("--data-dir", default=str(REPO / "data" / "agent_live_xauusd"))
    p.add_argument("--tfs", default="60,240")
    args = p.parse_args()

    start = _parse_day(args.start)
    end = _parse_day(args.end) + timedelta(hours=23, minutes=59)
    tfs = [int(x) for x in args.tfs.split(",") if x.strip()]

    macro = load_macro_frame(REPO / "data" / "macro")
    if not macro.names():
        macro = None
    levels = load_market_levels(LEVELS)
    calendar = NewsCalendar.load(NEWS)
    data_dir = Path(args.data_dir)
    fmap = {5: "5m", 15: "15m", 60: "60m", 240: "240m"}

    bars_by_tf, signals = {}, []
    for tf in tfs:
        path = data_dir / f"xauusd_{fmap[tf]}.csv"
        if not path.exists():
            print(f"WARN missing {path}", file=sys.stderr)
            continue
        bars = [b for b in load_bars_from_csv(path) if b.timestamp <= end]
        bars_by_tf[tf] = bars
        sigs = collect(bars, tf=tf, start=start, end=end, macro=macro,
                       levels=levels, calendar=calendar)
        signals.extend(sigs)
        print(f"M{tf}: {len(sigs)} grade A/B signals", flush=True)

    # resolve both models
    for s in signals:
        bars = bars_by_tf[s["tf"]]
        h = HORIZON.get(s["tf"], 480)
        s["res_structural"] = _resolve(bars, s["bar_idx"], s["side"], s["entry"],
                                       s["stop"], s["target_structural"], h)
        s["res_5r"] = _resolve(bars, s["bar_idx"], s["side"], s["entry"],
                               s["stop"], s["target_5r"], h)

    def grade_filter(s, g):
        return s["grade"] == "A" if g == "A" else (s["grade"] == "B" if g == "B" else s["grade"] in ("A", "B"))

    def tf_filter(s, t):
        return True if t == "all" else s["tf"] == t

    models = {"structural": "res_structural", "fixed_5r": "res_5r"}
    report = {
        "window": f"{start.date()} -> {end.date()}",
        "stop_model": "outside originating IFVG (plan.stop) -> loss = -1R",
        "horizon_bars": HORIZON,
        "note": "hit_rate/R/PF over RESOLVED trades; opens disclosed as n_open",
        "results": {},
    }
    label = {60: "1H", 240: "4H", "all": "both"}
    for mname, mkey in models.items():
        report["results"][mname] = {}
        for g in ("A", "B", "AB"):
            for t in (240, 60, "all"):
                if t != "all" and t not in tfs:
                    continue
                res = [s[mkey] for s in signals if grade_filter(s, g) and tf_filter(s, t)]
                report["results"][mname][f"{g}_{label[t]}"] = stat_block(res)

    out_json = REPO / "logs" / "ifvg_tp_structure_audit.json"
    out_json.write_text(json.dumps(report, indent=2))

    # console table
    for mname in models:
        print(f"\n=== {mname.upper()} TARGET ===")
        hdr = f"{'slice':<10}{'n':>5}{'open':>6}{'hit%':>7}{'avgR':>7}{'totR':>7}{'PF':>6}{'winRR':>7}{'BE%':>7}  clears"
        print(hdr)
        for k, v in report["results"][mname].items():
            if v["n"] == 0:
                print(f"{k:<10}{0:>5}{v['n_open']:>6}      -      -      -     -      -      -   -")
                continue
            thin = "*" if v["n"] < 20 else " "
            wr = v["avg_win_rr"]
            be = v["breakeven_wr"]
            wr_s = f"{wr:>7.2f}" if wr is not None else f"{'-':>7}"
            be_s = f"{be*100:>6.1f}" if be is not None else f"{'-':>6}"
            print(f"{k:<10}{v['n']:>4}{thin}{v['n_open']:>6}{v['hit_rate']*100:>6.1f}{v['avg_r']:>7.2f}"
                  f"{v['total_r']:>7.1f}{str(v['profit_factor']):>6}{wr_s}"
                  f"{be_s}  {'YES' if v['clears'] else 'no'}")
    print("\n* = n<20 (thin)")
    print(f"\nReport: {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
