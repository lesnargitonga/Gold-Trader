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


def log(msg: str) -> None:
    print(f"[absolute-gold] {msg}", flush=True)


def scout_loop() -> None:
    interval = int(os.getenv("GOLD_RENDER_SCOUT_INTERVAL_SECONDS", "300"))
    delay = int(os.getenv("GOLD_RENDER_SCOUT_INITIAL_DELAY_SECONDS", "5"))
    time.sleep(max(0, delay))
    while True:
        try:
            log("updating live context and running full-system IFVG engine")
            env = os.environ.copy()
            env.setdefault("GOLD_RUNTIME_ROOT", str(ROOT))
            env.setdefault("GOLD_TRADER_ROOT", str(ROOT))
            ctx = ROOT / "scripts" / "update_live_context.py"
            if ctx.exists():
                subprocess.run([sys.executable, str(ctx)], cwd=str(ROOT), check=False, env=env)
            subprocess.run([sys.executable, str(ROOT / "scripts" / "ifvg_full_system_engine.py")], cwd=str(ROOT), check=False, env=env)
            merge = ROOT / "scripts" / "merge_live_context_into_decision.py"
            if merge.exists():
                subprocess.run([sys.executable, str(merge)], cwd=str(ROOT), check=False, env=env)
        except Exception as exc:
            log(f"scout loop recovered after error: {exc!r}")
        time.sleep(interval)


def main() -> None:
    os.environ.setdefault("GOLD_EXECUTION_MODE", "paper")
    os.environ.setdefault("GOLD_ENABLE_LIVE_ORDERS", "false")
    os.environ.setdefault("GOLD_MARKET_DATA_PROVIDER", "twelvedata")
    os.environ.setdefault("GOLD_TRADER_ROOT", str(ROOT))
    thread = threading.Thread(target=scout_loop, daemon=True)
    thread.start()
    from gold_trader.web.absolute_gold_app import serve
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8770"))
    log(f"starting premium command center on {host}:{port}")
    log("scout loop enabled in background thread")
    serve(host=host, port=port)


if __name__ == "__main__":
    main()
