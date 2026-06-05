#!/usr/bin/env python3
"""Automatic IFVG scout with full-system decision layer."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from gold_trader.assistants.ifvg_scout import DEFAULT_SCAN_SECONDS, read_scout_timeframe, run_scout_scan, scout_log

POLL_SECONDS = int(os.environ.get("IFVG_SCOUT_INTERVAL", str(DEFAULT_SCAN_SECONDS)))
RESEARCH_EVERY_N = max(1, int(os.environ.get("IFVG_SCOUT_RESEARCH_EVERY", "3")))
FULL_ENGINE = REPO / "scripts" / "ifvg_full_system_engine.py"
UPDATER = REPO / "scripts" / "update_live_inputs.py"

def run_full_engine() -> None:
    # Run live-input updaters first (do not abort scout if they fail)
    try:
        subprocess.run([sys.executable, str(UPDATER)], cwd=str(REPO), check=False)
    except Exception as exc:
        scout_log(f"live inputs updater error: {exc!r}", level="WARNING")
    subprocess.run([sys.executable, str(FULL_ENGINE)], cwd=str(REPO), check=True)

    # Post-run: journal the decision, update paper outcomes, and refresh report.
    # Failures here should not kill the scout; just warn.
    post_steps = [
        REPO / "scripts" / "journal_decision_snapshot.py",
        REPO / "scripts" / "update_paper_signal_outcomes.py",
        REPO / "scripts" / "report_paper_performance.py",
    ]
    for step in post_steps:
        try:
            subprocess.run([sys.executable, str(step)], cwd=str(REPO), check=False)
        except Exception as exc:
            scout_log(f"post-engine step {step.name} failed: {exc!r}", level="WARNING")

def main() -> int:
    scout_log(f"IFVG full-system auto-scout starting · every {POLL_SECONDS}s")
    n = 0
    while True:
        try:
            n += 1
            run_full_engine()
            primary_tf = read_scout_timeframe()
            run_scout_scan(primary_tf=primary_tf, force_research=(n % RESEARCH_EVERY_N) == 0)
        except KeyboardInterrupt:
            scout_log("stopped")
            return 0
        except Exception as exc:
            scout_log(f"loop error: {exc!r}", level="ERROR")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    raise SystemExit(main())
