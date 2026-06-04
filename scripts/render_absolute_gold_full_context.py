#!/usr/bin/env python3
from __future__ import annotations

import json
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


def log(msg: str) -> None:
    print(f"[absolute-gold] {msg}", flush=True)


def run(cmd: list[str]) -> None:
    env = os.environ.copy()
    env.setdefault("GOLD_RUNTIME_ROOT", str(ROOT))
    env.setdefault("GOLD_TRADER_ROOT", str(ROOT))
    subprocess.run(cmd, cwd=str(ROOT), check=False, env=env)


def scout_loop() -> None:
    interval = int(os.getenv("GOLD_RENDER_SCOUT_INTERVAL_SECONDS", "300"))
    delay = int(os.getenv("GOLD_RENDER_SCOUT_INITIAL_DELAY_SECONDS", "5"))
    time.sleep(max(0, delay))
    while True:
        try:
            log("updating live context")
            run([sys.executable, "scripts/update_live_context.py"])
        except Exception as exc:
            log(f"live context update failed: {exc!r}")
        try:
            log("running full-system IFVG engine")
            run([sys.executable, "scripts/ifvg_full_system_engine.py"])
            run([sys.executable, "scripts/merge_live_context_into_decision.py"])
        except Exception as exc:
            log(f"IFVG engine failed: {exc!r}")
        try:
            from gold_trader.notify.telegram import send_decision_alert_if_needed

            p = ROOT / "logs" / "ifvg_mtf_decision_state.json"
            if p.exists():
                send_decision_alert_if_needed(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:
            log(f"alert dispatch skipped: {exc!r}")
        time.sleep(interval)


def main() -> None:
    os.environ.setdefault("GOLD_EXECUTION_MODE", "paper")
    os.environ.setdefault("GOLD_ENABLE_LIVE_ORDERS", "false")
    os.environ.setdefault("GOLD_MARKET_DATA_PROVIDER", "twelvedata")
    os.environ.setdefault("GOLD_RUNTIME_ROOT", str(ROOT))
    os.environ.setdefault("GOLD_TRADER_ROOT", str(ROOT))
    threading.Thread(target=scout_loop, daemon=True).start()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8770"))
    try:
        from gold_trader.web.absolute_gold_app import serve

        log(f"starting Absolute Gold app on {host}:{port}")
        log("scout loop enabled with full market awareness")
        serve(host=host, port=port)
    except ModuleNotFoundError:
        from gold_trader.web.command_center import serve

        log(f"starting command center on {host}:{port}")
        serve(host=host, port=port)


if __name__ == "__main__":
    main()
