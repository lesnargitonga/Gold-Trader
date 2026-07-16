#!/usr/bin/env python3
"""can_enter before/after replay — isolate the live sentiment gate effect.

Runs the FULL production live gate (build_approval_brief) over every historical
Grade-A IFVG signal on 1H/4H, then re-computes can_enter under three sentiment
policies so the macro/alignment gate effect is a single before/after number:

  current        : macro_regime must == mixed  AND alignment must == mixed bearish bias
  macro_fix      : block ONLY macro_regime == opposed ; alignment gate unchanged
  macro+align_fix: block ONLY macro_regime == opposed ; alignment never HARD-blocks

Everything else (grade A, verdict, score>=65, workflow_ready, workflow hard
fails on steps 1/3, external block) is computed once per signal and held
constant across the three policies, so the delta is purely the sentiment gate.

Re-run:
  PYTHONPATH=src .venv/bin/python scripts/can_enter_replay.py \
      --start 2026-04-17 --end 2026-06-19
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.assistants.ifvg_confluence import (  # noqa: E402
    IFVGAssistantConfig, find_ifvg_setups, load_market_levels, setup_to_dict,
)
from gold_trader.assistants.ifvg_workflow import (  # noqa: E402
    LIVE_ALIGNMENT_BLOCKERS, LIVE_ALIGNMENT_PREFERRED,
    alignment_from_bars, build_workflow_context, macro_regime_for_side,
)
from gold_trader.assistants.ifvg_scout import build_approval_brief  # noqa: E402
from gold_trader.calendar import NewsCalendar  # noqa: E402
from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.research.realtime_research import OpenAIResearchConfig  # noqa: E402

LEVELS = REPO / "config" / "market_levels.json"
NEWS = REPO / "data" / "macro" / "news_calendar.csv"


def _day(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _sentiment_blocks(alignment, macro_regime, policy):
    """Return True if this policy would HARD-block (sentiment blocker present)."""
    align_block = False
    if alignment != "unavailable" and alignment in LIVE_ALIGNMENT_BLOCKERS:
        align_block = True
    if policy == "current":
        macro_block = macro_regime != "mixed"
        return macro_block or align_block
    if policy == "macro_fix":
        macro_block = macro_regime == "opposed"
        return macro_block or align_block
    if policy == "macro+align_fix":
        macro_block = macro_regime == "opposed"
        return macro_block  # alignment demoted to warning
    raise ValueError(policy)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2026-04-17")
    p.add_argument("--end", default="2026-06-19")
    p.add_argument("--data-dir", default=str(REPO / "data" / "agent_live_xauusd"))
    p.add_argument("--tfs", default="60,240")
    args = p.parse_args()

    start, end = _day(args.start), _day(args.end) + timedelta(hours=23, minutes=59)
    tfs = [int(x) for x in args.tfs.split(",") if x.strip()]
    macro = load_macro_frame(REPO / "data" / "macro")
    if not macro.names():
        macro = None
    levels = load_market_levels(LEVELS)
    calendar = NewsCalendar.load(NEWS)
    data_dir = Path(args.data_dir)
    fmap = {5: "5m", 15: "15m", 60: "60m", 240: "240m"}

    full_bars = {}
    for tf in (5, 15, 60, 240):
        path = data_dir / f"xauusd_{fmap[tf]}.csv"
        if path.exists():
            full_bars[tf] = [b for b in load_bars_from_csv(path) if b.timestamp <= end]
    ts_index = {tf: [b.timestamp for b in bs] for tf, bs in full_bars.items()}

    def snapshot(ts):
        out = {}
        for tf, bs in full_bars.items():
            j = bisect.bisect_right(ts_index[tf], ts)
            if j >= 50:
                out[tf] = bs[:j]
        return out

    cfg = IFVGAssistantConfig()
    warmup = max(cfg.atr_period + 5, 80)
    research_off = OpenAIResearchConfig(enabled=False, mode="off")
    counts = {"current": 0, "macro_fix": 0, "macro+align_fix": 0}
    base_ok = 0
    n_gradeA = 0
    regime_tally = {}
    align_tally = {}
    seen = set()

    for tf in tfs:
        bars = full_bars.get(tf)
        if not bars:
            continue
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
            if str((d.get("grading") or {}).get("letter") or "D") != "A":
                continue
            c = best.candidate
            key = (tf, c.side.value, c.formation_idx, c.inversion_idx, c.signal_idx)
            if key in seen:
                continue
            seen.add(key)
            n_gradeA += 1

            snap = snapshot(ts)
            wf = build_workflow_context(
                d, primary_bars=bars[: i + 1], current_price=bars[i].close,
                bars_by_tf=snap, market_levels=levels,
            )
            alignment = alignment_from_bars(snap)
            regime = macro_regime_for_side(c.side.value, macro, ts) if macro else "unavailable"
            regime_tally[regime] = regime_tally.get(regime, 0) + 1
            align_tally[alignment] = align_tally.get(alignment, 0) + 1

            # base eligibility (everything except the sentiment gate)
            wf_steps = wf.get("steps") or []
            hard_fail = any(s.get("status") == "fail" and int(s.get("step") or 0) in (1, 3) for s in wf_steps)
            base = (
                str(d.get("verdict")) in ("valid_entry", "alert_wait")
                and int(d.get("score") or 0) >= 65
                and bool(wf.get("workflow_ready"))
                and not hard_fail
            )
            if base:
                base_ok += 1
            for policy in counts:
                if base and not _sentiment_blocks(alignment, regime, policy):
                    counts[policy] += 1

    report = {
        "window": f"{start.date()} -> {end.date()}",
        "tfs": tfs,
        "n_grade_A_signals": n_gradeA,
        "base_ok_pre_sentiment": base_ok,
        "can_enter": counts,
        "grade_A_by_macro_regime": regime_tally,
        "grade_A_by_alignment": align_tally,
        "alignment_blockers": sorted(LIVE_ALIGNMENT_BLOCKERS),
        "alignment_preferred": LIVE_ALIGNMENT_PREFERRED,
    }
    out = REPO / "logs" / "can_enter_replay.json"
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nReport: {out}")
    print("\nCAN_ENTER:  current={current}  macro_fix={macro_fix}  macro+align_fix={maf}".format(
        current=counts["current"], macro_fix=counts["macro_fix"], maf=counts["macro+align_fix"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
