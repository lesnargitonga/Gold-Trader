#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]

SCRIPTS = [
    ("market", REPO / "scripts" / "update_market_health_from_bridge.py"),
    ("macro", REPO / "scripts" / "update_macro_calendar.py"),
    ("sentiment", REPO / "scripts" / "update_sentiment_state.py"),
]


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run(script: Path) -> int:
    print(f"running {script}")
    return subprocess.run([sys.executable, str(script)], cwd=str(REPO)).returncode


def write_market_fallback(path: Path, error: str) -> None:
    ctx = {
        "state": "unknown",
        "source": "bridge",
        "updated_at": now(),
        "fresh": False,
        "spread_points": None,
        "error": error,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ctx, indent=2), encoding="utf-8")


def write_macro_fallback(path: Path, error: str) -> None:
    data = {
        "state": "unknown",
        "source": "none",
        "updated_at": now(),
        "fresh": False,
        "error": error,
        "events": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_sentiment_fallback(path: Path, error: str) -> None:
    data = {
        "state": "unknown",
        "score": 0,
        "confidence": 0,
        "fresh": False,
        "source": "none",
        "updated_at": now(),
        "error": error,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_json(path: Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def main() -> int:
    rc_total = 0
    # Run each updater, but don't abort on failure
    for name, script in SCRIPTS:
        try:
            rc = run(script)
            if rc != 0:
                print(f"script {script} exited {rc}")
                rc_total = rc_total or rc
                # write fallback normalized files
                if name == "market":
                    write_market_fallback(REPO / "logs" / "market_context_health.json", f"updater exit {rc}")
                if name == "macro":
                    write_macro_fallback(REPO / "data" / "macro" / "economic_calendar.json", f"updater exit {rc}")
                if name == "sentiment":
                    write_sentiment_fallback(REPO / "logs" / "sentiment_state.json", f"updater exit {rc}")
        except Exception as exc:
            print(f"error running {script}: {exc!r}")
            rc_total = rc_total or 1
            if name == "market":
                write_market_fallback(REPO / "logs" / "market_context_health.json", repr(exc))
            if name == "macro":
                write_macro_fallback(REPO / "data" / "macro" / "economic_calendar.json", repr(exc))
            if name == "sentiment":
                write_sentiment_fallback(REPO / "logs" / "sentiment_state.json", repr(exc))

    # Build summary
    market = load_json(REPO / "logs" / "market_context_health.json", {}) or {}
    macro = load_json(REPO / "data" / "macro" / "economic_calendar.json", {}) or {}
    sentiment = load_json(REPO / "logs" / "sentiment_state.json", {}) or {}

    market_health = "unknown"
    if isinstance(market, dict) and market.get("fresh"):
        # prefer live tick
        if market.get("spread_points") is not None:
            market_health = "fresh/live_tick"
        else:
            market_health = "fresh/unknown"
    elif isinstance(market, dict) and not market.get("fresh"):
        market_health = "stale"

    macro_state = "unknown"
    if isinstance(macro, dict) and macro.get("fresh"):
        macro_state = f"fresh/{macro.get('state','unknown')}"
    elif isinstance(macro, dict) and not macro.get("fresh"):
        macro_state = "stale/unknown"

    sentiment_state = "unknown"
    if isinstance(sentiment, dict) and sentiment.get("fresh"):
        sst = str(sentiment.get("state") or "unknown")
        sentiment_state = f"fresh/{sst}"
    elif isinstance(sentiment, dict) and not sentiment.get("fresh"):
        sentiment_state = "stale/unknown"

    summary = {
        "market_health": market_health,
        "macro": macro_state,
        "sentiment": sentiment_state,
        "ok_for_analysis": bool(market.get("fresh") and sentiment_state != "unknown"),
        "ok_for_live": False,
        "updated_at": now(),
    }

    print(json.dumps(summary, indent=2))
    return rc_total


if __name__ == "__main__":
    raise SystemExit(main())
