#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "logs" / "market_health.json"

def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def request_last_tick() -> dict:
    bridge_url = os.getenv("GOLD_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
    secret = os.getenv("GOLD_BRIDGE_SECRET", "").strip()

    req = urllib.request.Request(f"{bridge_url}/last-tick")
    if secret:
        req.add_header("X-Gold-Bridge-Secret", secret)
        req.add_header("X-GOLD-BRIDGE-SECRET", secret)

    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))

def build_health(tick: dict) -> dict:
    bid = float(tick["bid"])
    ask = float(tick["ask"])
    spread = ask - bid

    return {
        "updated_at": now(),
        "source": "mt5_bridge_last_tick",
        "symbol": tick.get("symbol"),
        "bid": bid,
        "ask": ask,
        "last": tick.get("last"),
        "spread_points": spread,
        "spread_source": "live_tick",
        "tick_timestamp": tick.get("ts") or tick.get("timestamp"),
        "bridge_status": "online",
    }

def main() -> None:
    try:
        tick = request_last_tick()
        health = build_health(tick)
    except Exception as exc:
        health = {
            "updated_at": now(),
            "source": "mt5_bridge_last_tick",
            "bridge_status": "error",
            "error": repr(exc),
            "spread_points": None,
            "spread_source": "unavailable",
        }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Write legacy market_health.json
    OUT.write_text(json.dumps(health, indent=2), encoding="utf-8")
    # Write normalized market_context_health.json
    ctx_path = OUT.parent / "market_context_health.json"
    ctx = {
        "state": "clear" if health.get("spread_points") is not None else "unknown",
        "source": "mt5_bridge_last_tick",
        "updated_at": health.get("updated_at"),
        "fresh": True if health.get("bridge_status") == "online" else False,
        "spread_points": health.get("spread_points"),
        "spread_source": health.get("spread_source"),
        "details": health,
    }
    ctx_path.write_text(json.dumps(ctx, indent=2), encoding="utf-8")
    print(json.dumps(health, indent=2))

if __name__ == "__main__":
    main()
