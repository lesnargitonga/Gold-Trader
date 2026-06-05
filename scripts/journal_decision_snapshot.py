#!/usr/bin/env python3
"""Write normalized decision snapshots to a line-delimited journal.

Reads `logs/ifvg_mtf_decision_state.json` and appends a compact, normalized
row to `logs/decision_journal.jsonl` and writes `logs/latest_decision_snapshot.json`.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
IN = REPO / "logs" / "ifvg_mtf_decision_state.json"
JOURNAL = REPO / "logs" / "decision_journal.jsonl"
LATEST = REPO / "logs" / "latest_decision_snapshot.json"


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize(decision: dict) -> dict:
    mc = (decision.get("market_context") or {})
    return {
        "timestamp_utc": decision.get("timestamp_utc") or now(),
        "symbol": decision.get("symbol") or os.getenv("GOLD_SYMBOL", "XAUUSD"),
        "action": decision.get("action"),
        "side": decision.get("side"),
        "grade": decision.get("final_grade") or decision.get("final_grade", ""),
        "score": decision.get("final_score") or 0,
        "current_price": decision.get("current_price"),
        "entry_low": decision.get("entry_low"),
        "entry_high": decision.get("entry_high"),
        "stop_loss": decision.get("stop_loss"),
        "tp1": decision.get("tp1"),
        "tp2": decision.get("tp2"),
        "tp3": decision.get("tp3"),
        "rr_tp1": decision.get("rr_tp1"),
        "rr_tp2": decision.get("rr_tp2"),
        "spread_points": mc.get("spread_points"),
        "spread_source": "live_tick" if mc.get("spread_points") is not None else "unknown",
        "session": mc.get("session"),
        "macro_state": mc.get("macro_state"),
        "sentiment_state": mc.get("sentiment_state"),
        "volatility_state": mc.get("volatility_state"),
        "paper_allowed": decision.get("paper_allowed"),
        "live_allowed": decision.get("live_allowed"),
        "blockers": decision.get("blockers") or mc.get("blockers") or [],
        "watching_for": decision.get("watching_for") or [],
    }


def main() -> None:
    if not IN.exists():
        print(f"no decision file: {IN}", file=sys.stderr)
        sys.exit(0)
    try:
        decision = json.loads(IN.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"failed to read decision file: {exc!r}", file=sys.stderr)
        sys.exit(1)

    row = normalize(decision)
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    LATEST.write_text(json.dumps(row, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(row, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
