#!/usr/bin/env python3
"""May 11 live XAUUSD manual-trade watcher.

Auto-trading stays disabled. This script only refreshes Dukascopy bars, evaluates
current manual-trade triggers, and writes alerts to logs/live_trade_watch.log.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.live.broker import BrokerError  # noqa: E402
from gold_trader.live.mt5_bridge_client import MT5RemoteBroker  # noqa: E402
from gold_trader.macro_filter import MacroDecisionFilter  # noqa: E402
from gold_trader.models import MarketBar, Side  # noqa: E402

OUTPUT_DIR = REPO / "data" / "manual_live_watch"
LOG_PATH = REPO / "logs" / "live_trade_watch.log"
STATE_PATH = REPO / "logs" / "live_trade_watch_state.json"
POLL_SECONDS = 300
BRIDGE_URL = os.environ.get("GOLD_BRIDGE_URL", "http://127.0.0.1:8765")
BRIDGE_SECRET = os.environ.get("GOLD_BRIDGE_SECRET", "")
BRIDGE_SYMBOL = os.environ.get("GOLD_SYMBOL", "GOLD")

# May 11 live structure from synchronized bars at 06:45 UTC.
SESSION_HIGH = 4705.565
SESSION_LOW = 4648.195
MIDPOINT = 4676.88
SHORT_REJECTION_LOW = 4683.65
SHORT_REJECTION_HIGH = 4705.565
SHORT_BREAKDOWN_LEVEL = 4648.195
LONG_RECLAIM_LEVEL = 4705.565
FRI_CLOSE = 4715.47


def log(message: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{ts}] {level:5s} {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as handle:
        handle.write(line + "\n")


def alert(tag: str, message: str) -> None:
    log(f">>> ALERT [{tag}] {message}", "ALERT")


def state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"fired": {}}


def save(s: dict) -> None:
    STATE_PATH.write_text(json.dumps(s, indent=2) + "\n")


def fire_once(s: dict, key: str) -> bool:
    if s["fired"].get(key):
        return False
    s["fired"][key] = datetime.now(timezone.utc).isoformat()
    save(s)
    return True


def fetch() -> None:
    cmd = [
        sys.executable,
        "-m",
        "gold_trader.cli",
        "sync-dukascopy",
        "--symbol",
        "XAUUSD",
        "--days",
        "4",
        "--base-interval-minutes",
        "15",
        "--timeframes",
        "15,60,240",
        "--max-workers",
        "4",
        "--output-dir",
        str(OUTPUT_DIR),
    ]
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
    if result.returncode != 0:
        log(f"sync failed rc={result.returncode}: {result.stderr[:300]}", "WARN")


def bridge() -> MT5RemoteBroker:
    return MT5RemoteBroker(base_url=BRIDGE_URL, shared_secret=BRIDGE_SECRET, timeout=5.0)


def bridge_ready() -> bool:
    try:
        data = bridge().healthz()
        return bool(data.get("ok"))
    except BrokerError:
        return False


def load_bridge(tf: str) -> list[MarketBar]:
    try:
        rows = bridge().get_candles(symbol=BRIDGE_SYMBOL, timeframe_minutes=int(tf), count=240)
    except (BrokerError, ValueError) as exc:
        log(f"bridge candle load failed tf={tf}m: {exc}", "WARN")
        return []
    bars: list[MarketBar] = []
    for row in rows:
        try:
            ts = datetime.fromtimestamp(float(row["time"]), timezone.utc)
            bars.append(MarketBar(
                timestamp=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0.0)),
                spread=float(row.get("spread", 0.0)),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return bars


def latest_csv(tf: str) -> Path | None:
    files = sorted(OUTPUT_DIR.glob(f"xauusd_*_{tf}m.csv"))
    return files[-1] if files else None


def load(tf: str) -> list:
    bridge_bars = load_bridge(tf)
    if bridge_bars:
        return bridge_bars
    path = latest_csv(tf)
    if not path or path.stat().st_size <= 100:
        return []
    return list(load_bars_from_csv(path))


def atr(bars: list, period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    prev = bars[-period - 1]
    values = []
    for bar in bars[-period:]:
        values.append(max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close)))
        prev = bar
    return sum(values) / period


def evaluate(s: dict, macro_filter: MacroDecisionFilter) -> None:
    bars15 = load("15")
    bars60 = load("60")
    if not bars15:
        log("no 15m bars loaded yet")
        return
    last = bars15[-1]
    last60 = bars60[-1] if bars60 else None
    atr15 = atr(bars15) or 9.3
    long_verdict = macro_filter.evaluate(Side.LONG, last.timestamp).verdict
    short_verdict = macro_filter.evaluate(Side.SHORT, last.timestamp).verdict

    log(
        f"15m {last.timestamp.isoformat()} O={last.open:.2f} H={last.high:.2f} "
        f"L={last.low:.2f} C={last.close:.2f} spread={last.spread:.3f} "
        f"macro_long={long_verdict} macro_short={short_verdict}"
    )

    gap_pct = (bars15[0].open - FRI_CLOSE) / FRI_CLOSE
    if abs(gap_pct) >= 0.006 and fire_once(s, "gap"):
        alert("GAP-DOWN REGIME", f"Session opened {gap_pct*100:+.2f}% from Friday close; favor confirmation, not chasing.")

    # Setup S1: sell rejection into 4683.65-4705.56, bearish close back under midpoint.
    body_bearish = last.close < last.open
    wick_into_zone = last.high >= SHORT_REJECTION_LOW
    close_under_mid = last.close < MIDPOINT
    if wick_into_zone and close_under_mid and body_bearish and fire_once(s, f"S1_{last.timestamp.isoformat()}"):
        stop = max(last.high, SHORT_REJECTION_HIGH) + 3.0
        risk = stop - last.close
        tp1 = SESSION_LOW
        rr = (last.close - tp1) / risk if risk > 0 else 0.0
        alert(
            "S1 SHORT REJECTION",
            f"Sell rejection: high {last.high:.2f} into {SHORT_REJECTION_LOW:.2f}-{SHORT_REJECTION_HIGH:.2f}, "
            f"close {last.close:.2f} under midpoint {MIDPOINT:.2f}. Entry~{last.close:.2f}, "
            f"stop~{stop:.2f}, TP1 {tp1:.2f}, TP2 4630/4618. RR~{rr:.2f}. Macro={short_verdict}."
        )

    # Setup S2: continuation breakdown below the session low.
    if last.close < SHORT_BREAKDOWN_LEVEL and fire_once(s, f"S2_{last.timestamp.isoformat()}"):
        stop = max(last.high, last.close + 1.5 * atr15)
        risk = stop - last.close
        tp1 = 4630.0
        rr = (last.close - tp1) / risk if risk > 0 else 0.0
        alert(
            "S2 SHORT BREAKDOWN",
            f"15m close {last.close:.2f} below session low {SHORT_BREAKDOWN_LEVEL:.2f}. "
            f"Entry~{last.close:.2f}, stop~{stop:.2f}, TP1 4630, TP2 4618/4600. RR~{rr:.2f}. Macro={short_verdict}."
        )

    # Setup L1: only consider a long if the market reclaims the whole sell zone.
    if last.close > LONG_RECLAIM_LEVEL and fire_once(s, f"L1_{last.timestamp.isoformat()}"):
        stop = MIDPOINT - 3.0
        risk = last.close - stop
        tp1 = 4729.0
        rr = (tp1 - last.close) / risk if risk > 0 else 0.0
        alert(
            "L1 LONG RECLAIM",
            f"15m close {last.close:.2f} reclaimed {LONG_RECLAIM_LEVEL:.2f}. Entry~{last.close:.2f}, "
            f"stop~{stop:.2f}, TP1 4729, TP2 4749/4764. RR~{rr:.2f}. Macro={long_verdict}; long is warning-only, reduce size."
        )

    if last60 and last60.close > LONG_RECLAIM_LEVEL and fire_once(s, f"L1_60_{last60.timestamp.isoformat()}"):
        alert(
            "60M LONG RECLAIM CONFIRM",
            f"60m close {last60.close:.2f} above {LONG_RECLAIM_LEVEL:.2f}; this upgrades long continuation quality, still macro={long_verdict}."
        )


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    s = state()
    macro_filter = MacroDecisionFilter(macro=load_macro_frame(REPO / "data" / "macro"))
    log("May 11 live trade watcher starting")
    log(f"levels: short zone {SHORT_REJECTION_LOW}-{SHORT_REJECTION_HIGH}, breakdown {SHORT_BREAKDOWN_LEVEL}, long reclaim {LONG_RECLAIM_LEVEL}")
    while True:
        try:
            if not bridge_ready():
                fetch()
            evaluate(s, macro_filter)
        except KeyboardInterrupt:
            log("stopped")
            return 0
        except Exception as exc:  # noqa: BLE001
            log(f"loop error: {exc!r}", "ERROR")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
