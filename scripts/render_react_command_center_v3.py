#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def log(msg: str) -> None:
    print(f"[render-react-v3] {msg}", flush=True)


def run_optional(script_rel: str, label: str) -> None:
    script = ROOT / script_rel
    try:
        if script.exists():
            env = os.environ.copy()
            env.setdefault("GOLD_RUNTIME_ROOT", str(ROOT))
            env.setdefault("GOLD_TRADER_ROOT", str(ROOT))
            log(label)
            subprocess.run([sys.executable, str(script)], cwd=str(ROOT), check=False, env=env)
    except Exception as exc:
        log(f"{label} failed: {exc!r}")


def scout_loop() -> None:
    interval = int(os.getenv("GOLD_RENDER_SCOUT_INTERVAL_SECONDS", "300"))
    delay = int(
        os.getenv(
            "GOLD_RENDER_SCOUT_STARTUP_DELAY_SECONDS",
            os.getenv("GOLD_INITIAL_SCAN_DELAY_SECONDS", os.getenv("GOLD_RENDER_SCOUT_INITIAL_DELAY_SECONDS", "6")),
        )
    )
    time.sleep(max(0, delay))
    while True:
        started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        log(f"cycle start {started}")
        run_optional("scripts/update_live_context.py", "updating live context")
        run_optional("scripts/ifvg_full_system_engine.py", "running full-system IFVG engine")
        run_optional("scripts/merge_live_context_into_decision.py", "merging live context into decision")
        try:
            from gold_trader.notify.telegram import send_decision_alert_if_needed
            import json

            from gold_trader.web.react_command_center import _latest_decision_path

            p = _latest_decision_path()
            if p and p.exists():
                send_decision_alert_if_needed(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:
            log(f"telegram alert skipped: {exc!r}")
        log(f"cycle complete; sleeping {interval}s")
        time.sleep(interval)


def main() -> None:
    os.environ.setdefault("GOLD_EXECUTION_MODE", "paper")
    os.environ.setdefault("GOLD_ENABLE_LIVE_ORDERS", "false")
    os.environ.setdefault("GOLD_MARKET_DATA_PROVIDER", "twelvedata")
    os.environ.setdefault("GOLD_RUNTIME_ROOT", str(ROOT))
    os.environ.setdefault("GOLD_TRADER_ROOT", str(ROOT))

    threading.Thread(target=scout_loop, daemon=True).start()

    from gold_trader.web.react_command_center import serve

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8770"))
    log("React command center v3 enabled")
    log("scout/context loop enabled in background thread")
    serve(host=host, port=port)


if __name__ == "__main__":
    main()
