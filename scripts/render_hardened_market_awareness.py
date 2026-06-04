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
    print(f"[market-awareness] {msg}", flush=True)


def run_optional(script_rel: str, label: str) -> None:
    script = ROOT / script_rel
    try:
        if script.exists():
            env = os.environ.copy()
            env.setdefault("GOLD_RUNTIME_ROOT", str(ROOT))
            env.setdefault("GOLD_TRADER_ROOT", str(ROOT))
            log(label)
            proc = subprocess.run([sys.executable, str(script)], cwd=str(ROOT), check=False, env=env)
            if proc.returncode:
                log(f"{label} exited {proc.returncode}")
    except Exception as exc:
        log(f"{label} failed: {exc!r}")


def scout_loop() -> None:
    interval = int(os.getenv("GOLD_RENDER_SCOUT_INTERVAL_SECONDS", "300"))
    calendar_every = int(os.getenv("GOLD_CALENDAR_REFRESH_EVERY_LOOPS", "12"))
    delay = int(
        os.getenv(
            "GOLD_RENDER_SCOUT_STARTUP_DELAY_SECONDS",
            os.getenv("GOLD_INITIAL_SCAN_DELAY_SECONDS", "6"),
        )
    )
    time.sleep(max(0, delay))
    n = 0
    while True:
        started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        log(f"cycle start {started}")
        try:
            if n % calendar_every == 0:
                run_optional("scripts/fetch_calendar.py", "fetching economic calendar")
            run_optional("scripts/update_live_context.py", "updating live context")
            run_optional("scripts/ifvg_full_system_engine.py", "running full-system IFVG engine")
            run_optional("scripts/merge_live_context_into_decision.py", "merging live context into decision")
            run_optional("scripts/harden_market_state.py", "hardening market state")
            run_optional("scripts/provider_health.py", "refreshing provider health")
            try:
                from gold_trader.notify.telegram import send_decision_alert_if_needed
                import json
                from gold_trader.core.market_state_hardening import find_decision_path

                p = find_decision_path()
                if p and p.exists():
                    send_decision_alert_if_needed(json.loads(p.read_text(encoding="utf-8")))
            except Exception as exc:
                log(f"telegram alert skipped: {exc!r}")
        except Exception as exc:
            log(f"cycle error: {exc!r}")
        n += 1
        log(f"cycle complete; sleeping {interval}s")
        time.sleep(interval)


def main() -> None:
    os.environ.setdefault("GOLD_EXECUTION_MODE", "paper")
    os.environ.setdefault("GOLD_ENABLE_LIVE_ORDERS", "false")
    os.environ.setdefault("GOLD_STRICT_UNKNOWN_CONTEXT", "true")
    os.environ.setdefault("GOLD_MARKET_DATA_PROVIDER", "twelvedata")
    os.environ.setdefault("GOLD_RUNTIME_ROOT", str(ROOT))
    os.environ.setdefault("GOLD_TRADER_ROOT", str(ROOT))

    threading.Thread(target=scout_loop, daemon=True).start()

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8770"))
    log(f"serving hardened market awareness on {host}:{port}")

    from gold_trader.web.command_center_v2 import serve

    serve(host=host, port=port)


if __name__ == "__main__":
    main()
