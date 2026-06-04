#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DECISION = REPO / "logs" / "ifvg_mtf_decision_state.json"
CONTEXT = REPO / "logs" / "live_market_context.json"


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> None:
    decision = load(DECISION)
    context = load(CONTEXT)
    if not decision or not context:
        return
    decision["live_market_context"] = context
    mc = decision.setdefault("market_context", {})
    for key in [
        "spread_points", "spread_state", "spread_source", "macro_state", "macro_source",
        "sentiment_score", "sentiment_state", "sentiment_source", "cot_state", "cot_source",
        "cross_market_state", "cross_market_source",
    ]:
        if key in context:
            mc[key] = context[key]
    blockers = decision.setdefault("blockers", [])
    for b in context.get("blockers", []):
        if b not in blockers:
            blockers.append(b)
    warnings = mc.setdefault("warnings", [])
    for w in context.get("warnings", []):
        if w not in warnings:
            warnings.append(w)
    notes = mc.setdefault("notes", [])
    for n in context.get("cross_market_notes", []):
        note = f"cross-market: {n}"
        if note not in notes:
            notes.append(note)
    DECISION.parent.mkdir(parents=True, exist_ok=True)
    DECISION.write_text(json.dumps(decision, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
