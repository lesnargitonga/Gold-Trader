#!/usr/bin/env python3
"""Automatic IFVG scout — always-on AI watch loop.

Scans live MT5 bars every ~60s, runs IFVG + OpenAI research when candidates
appear, writes model-facing alerts to logs/ifvg_scout_state.json.

Started automatically by ./start — no manual refresh needed.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.assistants.ifvg_scout import (  # noqa: E402
    DEFAULT_SCAN_SECONDS,
    read_scout_timeframe,
    run_scout_scan,
    scout_log,
)

POLL_SECONDS = int(os.environ.get("IFVG_SCOUT_INTERVAL", str(DEFAULT_SCAN_SECONDS)))
RESEARCH_EVERY_N = max(1, int(os.environ.get("IFVG_SCOUT_RESEARCH_EVERY", "3")))


def main() -> int:
    primary_tf = read_scout_timeframe()
    scout_log(f"IFVG auto-scout starting · M{primary_tf} every {POLL_SECONDS}s")
    n = 0
    while True:
        try:
            primary_tf = read_scout_timeframe()
            n += 1
            force_research = (n % RESEARCH_EVERY_N) == 0
            run_scout_scan(primary_tf=primary_tf, force_research=force_research)
        except KeyboardInterrupt:
            scout_log("stopped")
            return 0
        except Exception as exc:  # noqa: BLE001
            scout_log(f"loop error: {exc!r}", level="ERROR")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
