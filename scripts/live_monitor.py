#!/usr/bin/env python3
"""Live trade monitor for the 2026-05-11 session.

Polls Dukascopy every POLL_SECONDS, evaluates the discretionary trade plan
against the latest bars + macro filter, and writes alerts to
logs/live_monitor.log (plus stdout). Each trigger fires at most once.

Plan (from the analysis turn):
  A) Pullback LONG:    bullish 15m reversal inside 4694-4705, stop 4677, TP 4750/4764
                       (counter-macro — flagged half-size)
  B) Breakout LONG:    60m close > 4764 with body > 0.5 * ATR(14), stop 4735
  C) Failed-break SHORT: 15m close back below 4750 after wick > 4750,
                       OR 60m close back below 4715 after testing higher.
                       (macro-aligned)

Hard rules surfaced as warnings:
  - Skip first 15 min after open (wait one full 15m bar)
  - Gap > 0.6% triggers a HOLD-until-fill warning
  - Below 4677 = long thesis invalidated
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.macro_filter import MacroDecisionFilter  # noqa: E402
from gold_trader.models import Side  # noqa: E402

# --- plan constants ---------------------------------------------------------
PIVOT = 4715.0
FRI_HIGH = 4750.0
FRI_LOW = 4694.0
ASIA_LOW = 4677.0
SWING_HIGH = 4764.0
LONG_ZONE = (4694.0, 4705.0)
GAP_PCT = 0.006
LAST_CLOSE = 4715.47  # Fri 2026-05-08 20:00 UTC

POLL_SECONDS = 300  # 5 min
OUTPUT_DIR = REPO / "data" / "live_monitor"
LOG_PATH = REPO / "logs" / "live_monitor.log"
STATE_PATH = REPO / "logs" / "live_monitor_state.json"

# --- helpers ----------------------------------------------------------------
def log(msg: str, *, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{ts}] {level:5s} {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def alert(tag: str, msg: str) -> None:
    log(f">>> ALERT [{tag}] {msg}", level="ALERT")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"fired": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def fire_once(state: dict, key: str) -> bool:
    if state["fired"].get(key):
        return False
    state["fired"][key] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    return True


def fetch_bars() -> None:
    """Refresh 15m + 60m bars covering the last 3 days."""
    cmd = [
        sys.executable, "-m", "gold_trader.cli", "sync-dukascopy",
        "--symbol", "XAUUSD", "--days", "3",
        "--base-interval-minutes", "15",
        "--timeframes", "15,60",
        "--max-workers", "2",
        "--output-dir", str(OUTPUT_DIR),
    ]
    env = {"PYTHONPATH": str(REPO / "src")}
    import os
    env = {**os.environ, **env}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)
        if r.returncode != 0:
            log(f"sync-dukascopy failed rc={r.returncode}: {r.stderr[:200]}", level="WARN")
    except Exception as e:
        log(f"fetch error: {e}", level="WARN")


def latest_csv(tf: str) -> Optional[Path]:
    files = sorted(OUTPUT_DIR.glob(f"xauusd_*_{tf}m.csv"))
    return files[-1] if files else None


def atr(bars, period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs = []
    prev = bars[-period - 1]
    for b in bars[-period:]:
        tr = max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close))
        trs.append(tr)
        prev = b
    return sum(trs) / period


# --- evaluation -------------------------------------------------------------
@dataclass
class Snapshot:
    bars15: list = field(default_factory=list)
    bars60: list = field(default_factory=list)
    macro_filter: Optional[MacroDecisionFilter] = None


def evaluate(snap: Snapshot, state: dict) -> None:
    b15 = snap.bars15
    b60 = snap.bars60
    if not b15:
        log("no 15m bars yet (market likely pre-open)", level="DEBUG")
        return

    last15 = b15[-1]
    log(f"15m close={last15.close:.2f} hi={last15.high:.2f} lo={last15.low:.2f} ts={last15.timestamp.isoformat()}")

    # Open-gap check (first bar after weekend)
    first15 = b15[0]
    gap = (first15.open - LAST_CLOSE) / LAST_CLOSE
    if abs(gap) >= GAP_PCT and fire_once(state, "gap_open"):
        alert("GAP", f"Open gap {gap*100:+.2f}% (open={first15.open:.2f} vs Fri close {LAST_CLOSE:.2f}). Wait for fill or rejection before entering.")

    # Structural invalidation
    if last15.close < ASIA_LOW and fire_once(state, "invalidation"):
        alert("INVALIDATION", f"15m close {last15.close:.2f} < {ASIA_LOW} — long bias INVALIDATED. Skip Setup A & B.")

    # Macro filter on each candidate side at current bar
    long_v = snap.macro_filter.evaluate(Side.LONG, last15.timestamp) if snap.macro_filter else None
    short_v = snap.macro_filter.evaluate(Side.SHORT, last15.timestamp) if snap.macro_filter else None

    # -------- Setup A: Pullback LONG into 4694-4705 with bullish 15m reversal --
    in_zone = LONG_ZONE[0] <= last15.low <= LONG_ZONE[1] or LONG_ZONE[0] <= last15.close <= LONG_ZONE[1]
    if in_zone:
        # bullish reversal: close > open AND close in top 40% of range AND lower wick > body
        rng = last15.high - last15.low
        body = abs(last15.close - last15.open)
        lower_wick = min(last15.open, last15.close) - last15.low
        bullish = last15.close > last15.open and rng > 0 and (last15.close - last15.low) / rng >= 0.6 and lower_wick >= body
        if bullish and fire_once(state, f"A_{last15.timestamp.isoformat()}"):
            macro_note = f"macro={long_v.verdict if long_v else '?'}"
            alert("SETUP A — PULLBACK LONG",
                  f"Bullish 15m reversal in zone. Entry≈{last15.close:.2f} "
                  f"Stop {ASIA_LOW} TP1 {FRI_HIGH} TP2 {SWING_HIGH}+ "
                  f"R≈{(FRI_HIGH-last15.close)/(last15.close-ASIA_LOW):.2f}. {macro_note}. "
                  f"HALF SIZE (counter-macro).")

    # -------- Setup B: Breakout LONG on 60m close > 4764 with body > 0.5*ATR --
    if b60:
        last60 = b60[-1]
        atr60 = atr(b60, 14)
        if atr60 and last60.close > SWING_HIGH:
            body60 = abs(last60.close - last60.open)
            if body60 > 0.5 * atr60 and fire_once(state, f"B_{last60.timestamp.isoformat()}"):
                macro_note = f"macro={long_v.verdict if long_v else '?'}"
                alert("SETUP B — BREAKOUT LONG",
                      f"60m close {last60.close:.2f} > {SWING_HIGH} with body {body60:.2f} > 0.5*ATR {atr60:.2f}. "
                      f"Stop ≈4735, TP1 4800, TP2 4830 trail. {macro_note}.")

    # -------- Setup C: failed-breakout SHORT --------
    # 15m wick > 4750 then close < 4750
    if len(b15) >= 2:
        prev15 = b15[-2]
        wick_above = prev15.high > FRI_HIGH or last15.high > FRI_HIGH
        closed_back = last15.close < FRI_HIGH
        if wick_above and closed_back and last15.high > FRI_HIGH and last15.close < FRI_HIGH:
            if fire_once(state, f"C1_{last15.timestamp.isoformat()}"):
                stop = max(last15.high, prev15.high) + 5.0
                tp1 = FRI_LOW
                rr = (last15.close - tp1) / (stop - last15.close)
                macro_note = f"macro={short_v.verdict if short_v else '?'}"
                alert("SETUP C1 — FAILED BREAKOUT SHORT (15m)",
                      f"Wick > {FRI_HIGH} then close back below. Entry≈{last15.close:.2f} "
                      f"Stop≈{stop:.2f} TP1 {tp1} TP2 4650. R≈{rr:.2f}. {macro_note} (macro-aligned).")

    # 60m close back below pivot after testing higher
    if len(b60) >= 3:
        last60 = b60[-1]
        prev60 = b60[-2]
        if prev60.high > PIVOT + 10 and last60.close < PIVOT and last60.close < last60.open:
            if fire_once(state, f"C2_{last60.timestamp.isoformat()}"):
                stop = max(prev60.high, last60.high) + 5.0
                rr = (last60.close - FRI_LOW) / (stop - last60.close)
                macro_note = f"macro={short_v.verdict if short_v else '?'}"
                alert("SETUP C2 — FAILED BREAKOUT SHORT (60m)",
                      f"60m close {last60.close:.2f} back below pivot {PIVOT} after testing {prev60.high:.2f}. "
                      f"Stop≈{stop:.2f} TP1 {FRI_LOW} TP2 4650. R≈{rr:.2f}. {macro_note}.")


# --- main loop --------------------------------------------------------------
def main() -> int:
    log("live_monitor starting", level="INFO")
    log(f"plan: pivot={PIVOT} fri_high={FRI_HIGH} fri_low={FRI_LOW} asia_low={ASIA_LOW} swing_high={SWING_HIGH}")
    macro = load_macro_frame(REPO / "data" / "macro")
    mf = MacroDecisionFilter(macro=macro)
    state = load_state()

    while True:
        try:
            fetch_bars()
            csv15 = latest_csv("15")
            csv60 = latest_csv("60")
            snap = Snapshot(macro_filter=mf)
            if csv15 and csv15.stat().st_size > 100:
                snap.bars15 = list(load_bars_from_csv(csv15))
            if csv60 and csv60.stat().st_size > 100:
                snap.bars60 = list(load_bars_from_csv(csv60))
            log(f"loaded bars: 15m={len(snap.bars15)} 60m={len(snap.bars60)}")
            evaluate(snap, state)
        except KeyboardInterrupt:
            log("interrupted by user", level="INFO")
            return 0
        except Exception as e:
            log(f"loop error: {e!r}", level="ERROR")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
