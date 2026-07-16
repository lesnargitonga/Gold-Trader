#!/usr/bin/env python3
"""HTF IFVG edge audit (Task 1) — 4H / 1H IFVG with the macro live-gate OFF.

Answers one question honestly: does the IFVG scout strategy pay on 4H/1H
*before* the macro=mixed live gate is applied, and what is the sample size?

Methodology (documented so the numbers are defensible, not vibes):
  * Engine: gold_trader.assistants.ifvg_confluence.find_ifvg_setups — the same
    detector the live scout and Trade UI use.
  * Per-bar scan, point-in-time: HTF agreement is computed internally by the
    checklist at signal_idx (higher_timeframe_bias=None) so there is NO lookahead
    from a globally-computed bias.
  * OpenAI external research OFF → final grade letter == technical letter
    (A>=80, B 65-79, C 50-64, D <50). Same as live with research in soft/off.
  * Macro CSV still feeds the *checklist score* (macro_confirmation, up to 10pts),
    exactly as live. What is OFF here is the live *gate* (macro_regime=mixed
    required) — we never drop a signal for its regime; instead we TAG it.
  * Outcome: TP1-or-SL on the signal timeframe, R = realised move / initial risk
    (win uses true TP1 distance, loss = -1R). One-position-at-a-time per TF is
    reported alongside the all-signals (overlap) view.

Slices reported with n on every row:
  * by timeframe (60=1H, 240=4H)
  * by final grade (A, B)
  * by macro_regime (aligned / mixed / opposed / partial / unavailable)
  * grade x regime cross-tab (the slice the live gate actually keys on)

Re-run:
  PYTHONPATH=src .venv/bin/python scripts/ifvg_htf_edge_audit.py \
      --start 2026-04-17 --end 2026-06-19
"""
from __future__ import annotations

import argparse
import json
import statistics as stats
import sys
from collections import defaultdict
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
from gold_trader.assistants.ifvg_workflow import macro_regime_for_side  # noqa: E402
from gold_trader.calendar import NewsCalendar  # noqa: E402
from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.research.realtime_research import OpenAIResearchConfig  # noqa: E402

LEVELS = REPO / "config" / "market_levels.json"
NEWS = REPO / "data" / "macro" / "news_calendar.csv"
CONTRACT = 100.0
# horizon bars per TF ~ 7 calendar days of price action
HORIZON = {60: 168, 240: 42, 15: 672}


def _parse_day(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _simulate(bars, idx, *, side, entry, stop, tp1, max_bars):
    is_long = side == "long"
    end = min(len(bars), idx + 1 + max_bars)
    for j in range(idx + 1, end):
        bar = bars[j]
        if is_long:
            if bar.low <= stop:
                return "loss", stop, j
            if bar.high >= tp1:
                return "win", tp1, j
        else:
            if bar.high >= stop:
                return "loss", stop, j
            if bar.low <= tp1:
                return "win", tp1, j
    return "open", None, end - 1


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
            bars,
            index=i,
            macro_frame=macro,
            market_levels=levels,
            news_calendar=calendar,
            higher_timeframe_bias=None,  # point-in-time internal HTF, no lookahead
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
        regime = (
            macro_regime_for_side(c.side.value, macro, ts)
            if macro is not None
            else "unavailable"
        )
        plan = best.plan
        out.append({
            "time": ts.isoformat(),
            "tf": tf,
            "side": c.side.value,
            "score": best.score,
            "grade": letter,
            "verdict": best.verdict,
            "macro_regime": regime,
            "entry": plan.entry,
            "stop": plan.stop,
            "tp1": plan.tp1,
            "bar_idx": i,
        })
    return out


def settle(signals, bars_by_tf, *, one_position):
    rows = sorted(signals, key=lambda r: r["time"])
    busy_until = defaultdict(lambda: None)
    out: list[dict] = []
    for s in rows:
        tf = s["tf"]
        if one_position and busy_until[tf] and s["time"] <= busy_until[tf]:
            continue
        bars = bars_by_tf[tf]
        risk = abs(s["entry"] - s["stop"])
        if risk <= 0:
            continue
        outcome, exit_px, exit_idx = _simulate(
            bars, s["bar_idx"], side=s["side"], entry=s["entry"],
            stop=s["stop"], tp1=s["tp1"], max_bars=HORIZON.get(tf, 200),
        )
        if outcome == "open":
            continue
        if outcome == "win":
            move = (exit_px - s["entry"]) if s["side"] == "long" else (s["entry"] - exit_px)
            r = move / risk
        else:
            r = -1.0
        if one_position:
            busy_until[tf] = bars[exit_idx].timestamp.isoformat()
        out.append({**s, "outcome": outcome, "r": round(r, 3)})
    return out


def stat_block(trades):
    if not trades:
        return {"n": 0, "win_rate": None, "avg_r": None, "total_r": 0.0, "profit_factor": None}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    return {
        "n": len(trades),
        "win_rate": round(len(wins) / len(rs), 3),
        "avg_r": round(stats.mean(rs), 3),
        "total_r": round(sum(rs), 2),
        "profit_factor": (
            round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0
            else (None if not losses else 0.0)
        ),
    }


def slice_by(trades, key):
    groups = defaultdict(list)
    for t in trades:
        groups[t[key]].append(t)
    return {str(k): stat_block(v) for k, v in sorted(groups.items())}


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

    bars_by_tf: dict[int, list] = {}
    fmap = {5: "5m", 15: "15m", 60: "60m", 240: "240m"}
    for tf in tfs:
        path = data_dir / f"xauusd_{fmap[tf]}.csv"
        if not path.exists():
            print(f"WARN missing {path}", file=sys.stderr)
            continue
        full = load_bars_from_csv(path)
        bars_by_tf[tf] = [b for b in full if b.timestamp <= end]

    all_signals: list[dict] = []
    for tf, bars in bars_by_tf.items():
        sigs = collect(bars, tf=tf, start=start, end=end, macro=macro,
                       levels=levels, calendar=calendar)
        all_signals.extend(sigs)
        print(f"M{tf}: {len(sigs)} grade A/B signals", flush=True)

    overlap = settle(all_signals, bars_by_tf, one_position=False)
    sequential = settle(all_signals, bars_by_tf, one_position=True)

    def regime_cross(trades, grade):
        sub = [t for t in trades if t["grade"] == grade]
        return slice_by(sub, "macro_regime")

    report = {
        "window": f"{start.date()} -> {end.date()}",
        "macro_gate": "OFF (signals tagged by regime, never dropped)",
        "openai": "off",
        "htf": "point-in-time internal (no lookahead)",
        "signal_counts": {f"M{tf}": sum(1 for s in all_signals if s["tf"] == tf) for tf in tfs},
        "overlap_all": stat_block(overlap),
        "sequential_all": stat_block(sequential),
        "overlap": {
            "by_tf": slice_by(overlap, "tf"),
            "by_grade": slice_by(overlap, "grade"),
            "by_macro_regime": slice_by(overlap, "macro_regime"),
            "gradeA_by_regime": regime_cross(overlap, "A"),
            "gradeB_by_regime": regime_cross(overlap, "B"),
        },
        "sequential": {
            "by_tf": slice_by(sequential, "tf"),
            "by_grade": slice_by(sequential, "grade"),
            "by_macro_regime": slice_by(sequential, "macro_regime"),
            "gradeA_by_regime": regime_cross(sequential, "A"),
            "gradeB_by_regime": regime_cross(sequential, "B"),
        },
    }

    out_json = REPO / "logs" / "ifvg_htf_edge_audit.json"
    out_json.parent.mkdir(exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nReport: {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
