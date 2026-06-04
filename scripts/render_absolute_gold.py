#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)


def log(message: str) -> None:
    print(f"[absolute-gold] {message}", flush=True)


def run_once(label: str, cmd: list[str]) -> None:
    try:
        log(f"running {label}: {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=str(ROOT), check=False)
        if result.returncode:
            log(f"{label} exited with status {result.returncode}; continuing safely")
    except Exception as exc:
        log(f"{label} skipped: {exc!r}")


def scout_loop() -> None:
    interval = int(os.getenv("GOLD_RENDER_SCOUT_INTERVAL_SECONDS", "300"))
    n = 0
    while True:
        n += 1
        os.environ.setdefault("GOLD_EXECUTION_MODE", "paper")
        os.environ.setdefault("GOLD_ENABLE_LIVE_ORDERS", "false")
        # Optional live-context updater. It is allowed to be absent while the project is still integrating APIs.
        updater = ROOT / "scripts" / "update_live_context.py"
        if updater.exists():
            run_once("live-context", [sys.executable, str(updater)])
        run_once("full-system IFVG engine", [sys.executable, str(ROOT / "scripts" / "ifvg_full_system_engine.py")])
        # Legacy scout is useful locally, but must never break the cloud UI.
        legacy = ROOT / "scripts" / "ifvg_auto_scout.py"
        if os.getenv("GOLD_RUN_LEGACY_SCOUT", "false").lower() == "true" and legacy.exists():
            run_once("legacy scout", [sys.executable, str(legacy), "--once"])
        time.sleep(interval)


def main() -> None:
    os.environ.setdefault("GOLD_EXECUTION_MODE", "paper")
    os.environ.setdefault("GOLD_ENABLE_LIVE_ORDERS", "false")
    os.environ.setdefault("GOLD_MARKET_DATA_PROVIDER", "twelvedata")
    thread = threading.Thread(target=scout_loop, daemon=True)
    thread.start()
    from gold_trader.web.command_center import serve
    log("starting command center web + cloud scout")
    serve(host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8770")))


if __name__ == "__main__":
    main()
