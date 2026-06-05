#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "macro" / "economic_calendar.json"

def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def fetch_fmp_high_impact_usd(api_key: str) -> list[dict]:
    # Example FMP economic calendar endpoint (may require adjustments for FMP)
    # We'll request a short window around now and filter for USD + high impact
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    frm = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    to = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"https://financialmodelingprep.com/api/v3/economic_calendar?from={frm}&to={to}&apikey={api_key}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    # Expecting a list of events
    out = []
    for ev in data if isinstance(data, list) else []:
        try:
            impact = ev.get("impact") or ev.get("importance") or ev.get("significance") or ev.get("category")
            currency = ev.get("country") or ev.get("currency") or ""
            name = ev.get("event") or ev.get("title") or ev.get("name") or "macro event"
            time_utc = ev.get("date") or ev.get("time") or ev.get("datetime")
            out.append({"impact": impact, "currency": currency, "name": name, "time_utc": time_utc})
        except Exception:
            continue
    return out

def build_state(events: list[dict]) -> dict:
    state = "clear"
    next_event = None
    block_now = False
    now_dt = datetime.now(timezone.utc)
    for ev in events:
        impact = str(ev.get("impact") or ev.get("importance") or "").lower()
        if "high" in impact:
            et = ev.get("time_utc") or ev.get("datetime") or ev.get("time")
            next_event = ev
            block_now = True
            state = "blocked"
            break
    return {
        "state": state,
        "source": "fmp",
        "updated_at": now(),
        "fresh": True if events else False,
        "next_event": next_event,
        "block_now": block_now,
        "events": events,
    }

def main() -> None:
    api_key = os.getenv("FMP_API_KEY", "")
    events = []
    error = None
    try:
        if api_key:
            events = fetch_fmp_high_impact_usd(api_key)
        else:
            error = "no FMP_API_KEY"
    except Exception as exc:
        error = repr(exc)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if error:
        OUT.write_text(json.dumps({"state": "unknown", "source": "fmp", "updated_at": now(), "fresh": False, "error": error}, indent=2), encoding="utf-8")
        print(json.dumps({"error": error}, indent=2))
        return

    state = build_state(events)
    OUT.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(json.dumps(state, indent=2))

if __name__ == "__main__":
    main()
