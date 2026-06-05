#!/usr/bin/env python3
"""Scan the decision journal and track paper signal outcomes.

This script is paper-only. It reads `logs/decision_journal.jsonl` and updates
`logs/paper_signal_outcomes.jsonl` with tracked outcomes using bridge /candles
or TwelveData fallbacks.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
JOURNAL = REPO / "logs" / "decision_journal.jsonl"
OUT = REPO / "logs" / "paper_signal_outcomes.jsonl"
POLICY_PATH = REPO / "config" / "execution_policy.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_min_score() -> int:
    try:
        p = json.loads(POLICY_PATH.read_text(encoding="utf-8")) if POLICY_PATH.exists() else {}
        return int(p.get("minimum_final_score", 70))
    except Exception:
        return 70


def bridge_request(path: str, params: dict[str, Any]) -> Any:
    bridge_url = os.getenv("GOLD_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
    url = f"{bridge_url}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url)
    secret = os.getenv("GOLD_BRIDGE_SECRET", "").strip()
    if secret:
        req.add_header("X-GOLD-BRIDGE-SECRET", secret)
        req.add_header("X-Gold-Bridge-Secret", secret)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def to_epoch(ts: Any) -> int | None:
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return int(ts)
        s = str(ts)
        if s.isdigit():
            return int(s)
        # ISO parse
        try:
            dt = datetime.fromisoformat(s)
            return int(dt.timestamp())
        except Exception:
            # maybe milliseconds
            v = float(s)
            if v > 1e12:
                v = v / 1000.0
            return int(v)
    except Exception:
        return None


def numeric(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def fetch_candles_since(symbol: str, timeframe_min: int = 1, limit: int = 1440, since_epoch: int | None = None) -> list[dict]:
    try:
        rows = bridge_request("/candles", {"symbol": symbol, "timeframe": timeframe_min, "limit": limit})
        if not isinstance(rows, list):
            return []
        out = []
        for r in rows:
            t = to_epoch(r.get("time") or r.get("timestamp") or r.get("datetime") or r.get("time_utc") or r.get("ts"))
            if t is None:
                continue
            if since_epoch is not None and t <= since_epoch:
                continue
            out.append({
                "time": t,
                "open": numeric(r.get("open") or r.get("o")),
                "high": numeric(r.get("high") or r.get("h")),
                "low": numeric(r.get("low") or r.get("l")),
                "close": numeric(r.get("close") or r.get("c")),
            })
        # ensure chronological
        out.sort(key=lambda x: x["time"])
        return out
    except Exception:
        return []


def compute_r_metrics(signal: dict, candles: list[dict]) -> dict:
    # default values
    out = {
        "status": signal.get("status", "open"),
        "max_favorable_r": float(signal.get("max_favorable_r", 0.0) or 0.0),
        "max_adverse_r": float(signal.get("max_adverse_r", 0.0) or 0.0),
        "first_outcome": signal.get("first_outcome", "none"),
        "last_checked_utc": now_iso(),
    }

    entry = signal.get("entry_reference")
    sl = signal.get("stop_loss")
    tp1 = signal.get("tp1")
    tp2 = signal.get("tp2")
    tp3 = signal.get("tp3")
    side = signal.get("side")

    if entry is None or sl is None or side not in ("buy", "sell"):
        out["status"] = signal.get("status", "open")
        return out

    try:
        R = abs(float(entry) - float(sl))
        if R <= 0:
            return out
    except Exception:
        return out

    first = out["first_outcome"]
    max_fav = out["max_favorable_r"]
    max_adv = out["max_adverse_r"]
    status = out["status"]

    prev_close = None
    for c in candles:
        op = c.get("open") if c.get("open") is not None else prev_close
        hi = c.get("high")
        lo = c.get("low")
        cl = c.get("close")
        prev_close = cl

        if hi is None or lo is None:
            continue

        # compute favorable/adverse for this candle
        if side == "sell":
            fav = (float(entry) - lo) / R if lo is not None else 0.0
            adv = (hi - float(entry)) / R if hi is not None else 0.0
        else:
            fav = (hi - float(entry)) / R if hi is not None else 0.0
            adv = (float(entry) - lo) / R if lo is not None else 0.0

        if fav > max_fav:
            max_fav = fav
        if adv > max_adv:
            max_adv = adv

        # check TP hits and SL hits
        def hit_price_in_candle(hit_price: float) -> bool:
            return (lo <= hit_price <= hi) if lo is not None and hi is not None else False

        # check ordered TP hits
        hit_tp = None
        for tp in (tp1, tp2, tp3):
            if tp is None:
                continue
            if hit_price_in_candle(float(tp)):
                hit_tp = tp
                break

        hit_sl = hit_price_in_candle(float(sl)) if sl is not None else False

        if hit_tp and not hit_sl:
            status = f"tp{1 if hit_tp==tp1 else 2 if hit_tp==tp2 else 3}_hit"
            if first == "none":
                first = f"tp{1 if hit_tp==tp1 else 2 if hit_tp==tp2 else 3}"
            # stop at first closed outcome
            out.update({"status": status, "max_favorable_r": round(max_fav, 4), "max_adverse_r": round(max_adv,4), "first_outcome": first, "last_checked_utc": now_iso()})
            return out

        if hit_sl and not hit_tp:
            status = "sl_hit"
            if first == "none":
                first = "sl"
            out.update({"status": status, "max_favorable_r": round(max_fav,4), "max_adverse_r": round(max_adv,4), "first_outcome": first, "last_checked_utc": now_iso()})
            return out

        if hit_sl and hit_tp:
            # ambiguous: decide by distance from open if available
            if op is not None:
                try:
                    opv = float(op)
                    tp_dist = abs(opv - float(hit_tp))
                    sl_dist = abs(opv - float(sl))
                    if tp_dist <= sl_dist:
                        first = f"tp{1 if hit_tp==tp1 else 2 if hit_tp==tp2 else 3}"
                        status = f"tp{1 if hit_tp==tp1 else 2 if hit_tp==tp2 else 3}_hit"
                    else:
                        first = "sl"
                        status = "sl_hit"
                except Exception:
                    first = "none"
                    status = "expired"
            else:
                first = "none"
                status = "expired"
            out.update({"status": status, "max_favorable_r": round(max_fav,4), "max_adverse_r": round(max_adv,4), "first_outcome": first, "last_checked_utc": now_iso()})
            return out

    # no TP/SL hit in scanned candles
    out.update({"status": status, "max_favorable_r": round(max_fav,4), "max_adverse_r": round(max_adv,4), "first_outcome": first, "last_checked_utc": now_iso()})
    return out


def load_existing() -> dict:
    ret = {}
    if not OUT.exists():
        return ret
    try:
        for line in OUT.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            key = f"{obj.get('signal_timestamp_utc')}|{obj.get('symbol')}"
            ret[key] = obj
    except Exception:
        return {}
    return ret


def write_all(outmap: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for k, v in outmap.items():
            fh.write(json.dumps(v, ensure_ascii=False) + "\n")


def main() -> None:
    if not JOURNAL.exists():
        print("no decision journal to scan", file=sys.stderr)
        return

    min_score = load_min_score()
    existing = load_existing()

    # gather candidate signals
    candidates = []
    for line in JOURNAL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        action = (row.get("action") or "").upper()
        score = int(row.get("score") or 0)
        paper_allowed = row.get("paper_allowed") is True
        qualifies = ("TRADE_READY" in action) or (paper_allowed and score >= min_score)
        if not qualifies:
            continue

        key = f"{row.get('timestamp_utc')}|{row.get('symbol')}"
        # entry reference: midpoint of entry_low/entry_high if available, else current_price
        entry_low = row.get("entry_low")
        entry_high = row.get("entry_high")
        if entry_low is not None and entry_high is not None:
            try:
                entry_ref = (float(entry_low) + float(entry_high)) / 2.0
            except Exception:
                entry_ref = row.get("current_price")
        else:
            entry_ref = row.get("current_price") or entry_low or entry_high

        base = existing.get(key) or {
            "signal_timestamp_utc": row.get("timestamp_utc"),
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "entry_reference": entry_ref,
            "stop_loss": row.get("stop_loss"),
            "tp1": row.get("tp1"),
            "tp2": row.get("tp2"),
            "tp3": row.get("tp3"),
            "status": "open",
            "max_favorable_r": 0.0,
            "max_adverse_r": 0.0,
            "first_outcome": "none",
            "last_checked_utc": None,
            # helpful metadata for grouping
            "grade": row.get("grade"),
            "session": row.get("session"),
            "macro_state": row.get("macro_state"),
            "sentiment_state": row.get("sentiment_state"),
        }

        candidates.append((key, base, row))

    # process each candidate and update outcomes
    outmap = existing.copy()
    for key, base, row in candidates:
        # if already closed, skip update but keep record (we still refresh last_checked)
        try:
            sig_epoch = None
            try:
                sig_epoch = int(datetime.fromisoformat(base.get("signal_timestamp_utc")).timestamp())
            except Exception:
                sig_epoch = None

            candles = fetch_candles_since(base.get("symbol"), timeframe_min=1, limit=1440, since_epoch=sig_epoch)
            metrics = compute_r_metrics(base, candles)
            base.update(metrics)
            # copy back helpful metadata
            base["grade"] = row.get("grade")
            base["session"] = row.get("session")
            base["macro_state"] = row.get("macro_state")
            base["sentiment_state"] = row.get("sentiment_state")
            outmap[key] = base
        except Exception as exc:
            # do not fail — log and continue
            print(f"warning: failed processing {key}: {exc!r}", file=sys.stderr)

    write_all(outmap)
    print(f"updated {len(outmap)} paper outcomes")


if __name__ == "__main__":
    main()
