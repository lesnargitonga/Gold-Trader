#!/usr/bin/env python3
"""Make the corrected IFVG scout the authoritative decision the Command Center shows.

Reads logs/ifvg_scout_state.json (corrected macro/alignment gates + per-slice 5R
routing, on live MT5 data) and writes logs/ifvg_mtf_decision_state.json in the
schema the publisher + Render command center expect. The publisher
(publish_state_to_render_payload.py) then packages it into
data/cloud_state/latest_cloud_state.json which the UI reads.

Run on a short loop (see scripts/local_up_8766.sh) so the dashboard tracks the
scout instead of the Twelve-Data hardening engine.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCOUT = REPO / "logs" / "ifvg_scout_state.json"
DECISION = REPO / "logs" / "ifvg_mtf_decision_state.json"

_SIDE = {"long": "buy", "short": "sell"}
_BIAS = {"bullish": "bullish", "bearish": "bearish", "ranging": "mixed",
         "mixed": "mixed", "unknown": "unknown", "neutral": "mixed"}


def _bias(v: str) -> str:
    return _BIAS.get(str(v or "").lower(), "unknown")


def build_decision(state: dict) -> dict:
    brief = state.get("approval_brief") or {}
    setup = state.get("setup") or {}
    wf = state.get("workflow") or {}
    htf = wf.get("htf_bias") or {}
    trade = brief.get("trade") or {}
    plan = setup.get("entry_plan") or {}

    side_raw = str(setup.get("side") or "").lower()
    side = _SIDE.get(side_raw, "none")
    grade = str(brief.get("final_grade") or setup.get("grade") or "--")
    score = int(setup.get("score") or 0)
    can_enter = bool(brief.get("can_enter"))
    bridge_online = bool(state.get("bridge_online"))
    price = state.get("current_price")
    tf = int(setup.get("timeframe_minutes") or state.get("primary_timeframe") or 60)

    # Routed 5R target is the headline; structural levels kept as secondary.
    tp_target = brief.get("tp_target")
    tp1 = tp_target if tp_target is not None else trade.get("tp1")
    tp2 = trade.get("tp1") if tp_target is not None else trade.get("tp2")
    tp3 = trade.get("tp2") if tp_target is not None else trade.get("tp3")

    entry = plan.get("entry")
    stop = trade.get("stop")
    rr_tp1 = None
    if entry is not None and stop is not None and tp1 is not None and abs(entry - stop) > 0:
        rr_tp1 = round(abs(tp1 - entry) / abs(entry - stop), 2)

    action = "TRADE_READY_PAPER_AUTO_ALERT_AUTO" if can_enter else "WAIT"

    # reasons (scout dicts) -> strings; watching_for from model_watch
    reasons = []
    for r in (brief.get("reasons") or []):
        if isinstance(r, dict):
            reasons.append(f"{r.get('title','')}: {r.get('detail','')}".strip(": "))
        else:
            reasons.append(str(r))
    blockers = [str(b) for b in (brief.get("blockers") or [])]
    watching = [str(w) for w in (brief.get("model_watch") or [])]

    tf_label = {240: "H4", 60: "H1", 30: "M30", 15: "M15", 5: "M5", 1: "M1"}.get(tf, f"M{tf}")
    timeframe_reads = [
        {"timeframe": "H4", "bias": _bias(htf.get("h4")), "ifvg_side": "none",
         "score": 0, "candles": 0, "aligned": _bias(htf.get("h4")) in ("bullish", "bearish")},
        {"timeframe": tf_label, "bias": _bias(htf.get("h1")) if tf == 60 else _bias(side_raw),
         "ifvg_side": side, "score": score, "candles": 0, "aligned": score >= 50},
    ]

    spread_health = "live_tick" if bridge_online else "unavailable"
    return {
        "source": "local_authoritative_engine",
        "symbol": "XAUUSD",
        "symbol_display": "XAU/USD",
        "broker_symbol": "GOLD",
        "timestamp_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "action": action,
        "side": side,
        "final_grade": grade,
        "final_score": score,
        "current_price": price,
        "entry_low": trade.get("entry_low"),
        "entry_high": trade.get("entry_high"),
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rr_tp1": rr_tp1,
        "rr_tp2": None,
        "tp_model": brief.get("tp_model"),
        "live_track": brief.get("live_track"),
        "live_allowed": False,
        "live_orders_enabled": False,
        "paper_allowed": can_enter,
        "next_update": (str(brief.get("headline") or "") + " — " + str(brief.get("summary") or "")).strip(" —"),
        "operator_message": str(brief.get("headline") or "Scanning"),
        "reasons": reasons,
        "blockers": blockers,
        "watching_for": watching,
        "timeframe_reads": timeframe_reads,
        "market_context": {
            "spread_source": "live_tick" if bridge_online else "unavailable",
            "session": "unknown",
            "data_provider": "mt5_bridge_local" if bridge_online else "csv_fallback",
        },
        "data_health": {
            "spread": spread_health,
            "decision": "fresh",
            "data_source": state.get("data_source"),
        },
        "cloud_status": {
            "broker": "MT5 bridge local",
            "orders": "locked",
            "data_provider": "local_authoritative_engine",
            "execution_mode": "paper",
        },
        "engine": "ifvg_scout_corrected",
    }


def main() -> int:
    if not SCOUT.exists():
        print("no scout state yet")
        return 1
    state = json.loads(SCOUT.read_text())
    decision = build_decision(state)
    tmp = DECISION.with_suffix(".tmp")
    tmp.write_text(json.dumps(decision, indent=2))
    tmp.replace(DECISION)
    print(f"decision written: action={decision['action']} grade={decision['final_grade']} "
          f"score={decision['final_score']} side={decision['side']} src={decision['data_health']['data_source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
