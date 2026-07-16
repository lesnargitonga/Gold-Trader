#!/usr/bin/env python3
"""Task 2 before/after — per-slice TP routing eligibility on the 63-day replay.

Runs the REAL routed build_approval_brief over every Grade A/B IFVG signal on
1H/4H and tallies can_enter by grade x timeframe, so the routing change is a
concrete before/after:

  before (A-only)  : Grade A eligible on any TF; Grade B never eligible.
  after  (routing) : config/tp_routing.json -> A all TFs (5R, live-track);
                     B on 4H only (5R, paper, off live-track); B-1H blocked.

Re-run:
  PYTHONPATH=src .venv/bin/python scripts/tp_routing_replay.py
"""
from __future__ import annotations

import bisect
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.assistants.ifvg_confluence import (  # noqa: E402
    IFVGAssistantConfig, find_ifvg_setups, load_market_levels, setup_to_dict,
)
from gold_trader.assistants.ifvg_scout import build_approval_brief  # noqa: E402
from gold_trader.assistants.ifvg_workflow import (  # noqa: E402
    alignment_from_bars, build_workflow_context, evaluate_live_sentiment, macro_regime_for_side,
)
from gold_trader.calendar import NewsCalendar  # noqa: E402
from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.research.realtime_research import OpenAIResearchConfig  # noqa: E402

LEVELS = REPO / "config" / "market_levels.json"
NEWS = REPO / "data" / "macro" / "news_calendar.csv"


def main() -> int:
    start = datetime(2026, 4, 17, tzinfo=timezone.utc)
    end = datetime(2026, 6, 19, 23, 59, tzinfo=timezone.utc)
    macro = load_macro_frame(REPO / "data" / "macro")
    if not macro.names():
        macro = None
    levels = load_market_levels(LEVELS)
    calendar = NewsCalendar.load(NEWS)
    data_dir = REPO / "data" / "agent_live_xauusd"
    fmap = {5: "5m", 15: "15m", 60: "60m", 240: "240m"}
    full = {}
    for tf in (5, 15, 60, 240):
        p = data_dir / f"xauusd_{fmap[tf]}.csv"
        if p.exists():
            full[tf] = [b for b in load_bars_from_csv(p) if b.timestamp <= end]
    tsx = {tf: [b.timestamp for b in bs] for tf, bs in full.items()}

    def snap(ts):
        out = {}
        for tf, bs in full.items():
            j = bisect.bisect_right(tsx[tf], ts)
            if j >= 50:
                out[tf] = bs[:j]
        return out

    cfg = IFVGAssistantConfig()
    warmup = max(cfg.atr_period + 5, 80)
    off = OpenAIResearchConfig(enabled=False, mode="off")
    seen = set()
    can_enter_after = defaultdict(int)   # (grade, tf)
    can_enter_before = defaultdict(int)  # A-only baseline
    sig = defaultdict(int)

    for tf in (60, 240):
        bars = full.get(tf)
        if not bars:
            continue
        for i in range(warmup, len(bars)):
            ts = bars[i].timestamp
            if ts < start or ts > end:
                continue
            setups = find_ifvg_setups(bars, index=i, macro_frame=macro, market_levels=levels,
                                      news_calendar=calendar, higher_timeframe_bias=None,
                                      openai_research_config=off)
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
            sig[(letter, tf)] += 1
            sn = snap(ts)
            wf = build_workflow_context(d, primary_bars=bars[: i + 1], current_price=bars[i].close,
                                        bars_by_tf=sn, market_levels=levels)
            wf["live_sentiment"] = evaluate_live_sentiment(
                side=c.side.value, alignment=alignment_from_bars(sn),
                macro_regime=macro_regime_for_side(c.side.value, macro, ts) if macro else "unavailable",
            )
            brief = build_approval_brief(d, wf)
            if brief["can_enter"]:
                can_enter_after[(letter, tf)] += 1
                if letter == "A":
                    can_enter_before[(letter, tf)] += 1

    def fmt(dct):
        return {f"{g}-M{tf}": dct[(g, tf)] for (g, tf) in sorted(dct)}

    report = {
        "window": "2026-04-17 -> 2026-06-19",
        "signals_by_slice": fmt(sig),
        "can_enter_before_A_only": fmt(can_enter_before),
        "can_enter_before_total": sum(can_enter_before.values()),
        "can_enter_after_routing": fmt(can_enter_after),
        "can_enter_after_total": sum(can_enter_after.values()),
    }
    out = REPO / "logs" / "tp_routing_replay.json"
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
