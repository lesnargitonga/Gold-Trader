#!/usr/bin/env python3
"""Render entrypoint: web UI + IFVG full-system scout in one process."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FULL_ENGINE = REPO / "scripts" / "ifvg_full_system_engine.py"
SCOUT_INTERVAL = int(os.environ.get("GOLD_RENDER_SCOUT_INTERVAL_SECONDS", "300"))


def log_render(msg: str) -> None:
    print(f"[render] {msg}", flush=True)


def log_scout(msg: str) -> None:
    print(f"[render-scout] {msg}", flush=True)


def run_full_engine() -> None:
    log_scout("running full-system IFVG engine")
    subprocess.run([sys.executable, str(FULL_ENGINE)], cwd=str(REPO), check=False)


def scout_loop() -> None:
    from gold_trader.assistants.ifvg_scout import read_scout_timeframe, run_scout_scan

    research_every = max(1, int(os.environ.get("IFVG_SCOUT_RESEARCH_EVERY", "3")))
    n = 0
    while True:
        try:
            n += 1
            run_full_engine()
            primary_tf = read_scout_timeframe()
            run_scout_scan(primary_tf=primary_tf, force_research=(n % research_every) == 0)
        except Exception as exc:
            log_scout(f"loop error: {exc!r}")
        time.sleep(SCOUT_INTERVAL)


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("GOLD_WEB_PORT", "8770")))
    log_render(f"starting web on {host}:{port}")
    threading.Thread(target=scout_loop, name="ifvg-scout", daemon=True).start()
    log_render("scout loop enabled in background thread")
    from gold_trader.web import serve

    serve(host=host, port=port)


if __name__ == "__main__":
    raise SystemExit(main())
