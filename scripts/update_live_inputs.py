#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SCRIPTS = [
    REPO / "scripts" / "update_market_health_from_bridge.py",
    REPO / "scripts" / "update_macro_calendar.py",
    REPO / "scripts" / "update_sentiment_state.py",
]

def run(script: Path) -> int:
    print(f"running {script}")
    return subprocess.run([sys.executable, str(script)], cwd=str(REPO)).returncode

def main() -> int:
    rc = 0
    for s in SCRIPTS:
        try:
            rc = run(s)
            if rc != 0:
                print(f"script {s} exited {rc}")
        except Exception as exc:
            print(f"error running {s}: {exc!r}")
    return rc

if __name__ == "__main__":
    raise SystemExit(main())
