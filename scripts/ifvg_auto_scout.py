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
JOURNAL = REPO / "scripts" / "journal_decision_snapshot.py"
OUTCOMES = REPO / "scripts" / "update_paper_signal_outcomes.py"
REPORT = REPO / "scripts" / "report_paper_performance.py"
PUBLISH_PAYLOAD = REPO / "scripts" / "publish_state_to_render_payload.py"
PUBLISH_RENDER = REPO / "scripts" / "publish_state_to_render.py"


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(REPO / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src if not existing else f"{src}{os.pathsep}{existing}"
    return env


def run_step(step: Path) -> None:
    try:
        result = subprocess.run([sys.executable, str(step)], cwd=str(REPO), env=child_env(), check=False)
    except Exception as exc:
        scout_log(f"decision refresh step {step.name} failed: {exc!r}", level="WARNING")
        return
    if result.returncode != 0:
        scout_log(f"decision refresh step {step.name} exited {result.returncode}", level="WARNING")


def run_full_engine() -> None:
    for step in (UPDATER, FULL_ENGINE, JOURNAL, OUTCOMES, REPORT, PUBLISH_PAYLOAD):
        run_step(step)
    if os.environ.get("GOLD_RENDER_INGEST_URL") and os.environ.get("GOLD_CLOUD_SYNC_TOKEN"):
        run_step(PUBLISH_RENDER)

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
