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
    print(f"[react-render] {msg}", flush=True)


def run_optional(script_rel: str) -> None:
    script = ROOT / script_rel
    try:
        if script.exists():
            env = os.environ.copy()
            env.setdefault("GOLD_RUNTIME_ROOT", str(ROOT))
            env.setdefault("GOLD_TRADER_ROOT", str(ROOT))
            subprocess.run([sys.executable, str(script)], cwd=str(ROOT), check=False, env=env)
    except Exception as exc:
        log(f"{script_rel} skipped: {exc!r}")


def scout_loop() -> None:
    interval = int(os.getenv("GOLD_RENDER_SCOUT_INTERVAL_SECONDS", "300"))
    delay = int(os.getenv("GOLD_RENDER_SCOUT_INITIAL_DELAY_SECONDS", "5"))
    os.environ.setdefault("GOLD_EXECUTION_MODE", "paper")
    os.environ.setdefault("GOLD_ENABLE_LIVE_ORDERS", "false")
    time.sleep(max(0, delay))
    while True:
        started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        log(f"cycle start {started}")
        run_optional("scripts/update_live_context.py")
        run_optional("scripts/ifvg_full_system_engine.py")
        run_optional("scripts/merge_live_context_into_decision.py")
        try:
            from gold_trader.notify.telegram import send_decision_alert_if_needed
            import json

            p = ROOT / "logs" / "ifvg_mtf_decision_state.json"
            if p.exists():
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
    log(f"starting React command center on {host}:{port}")
    log("scout loop enabled in background thread")
    serve(host=host, port=port)


if __name__ == "__main__":
    main()
