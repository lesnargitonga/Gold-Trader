#!/usr/bin/env python3
"""Generate paper performance reports from `logs/paper_signal_outcomes.jsonl`.

Writes `logs/paper_performance_report.json` and `logs/paper_performance_report.md`.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from statistics import mean
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
IN = REPO / "logs" / "paper_signal_outcomes.jsonl"
OUT_JSON = REPO / "logs" / "paper_performance_report.json"
OUT_MD = REPO / "logs" / "paper_performance_report.md"


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_outcomes():
    if not IN.exists():
        return []
    out = []
    for line in IN.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def compute_metrics(rows: list[dict]) -> dict:
    total = len(rows)
    open_signals = [r for r in rows if r.get("status") == "open"]
    closed = [r for r in rows if r.get("status") != "open"]

    def count_status(st):
        return len([r for r in closed if r.get("status") == st])

    tp1 = count_status("tp1_hit")
    tp2 = count_status("tp2_hit")
    tp3 = count_status("tp3_hit")
    sl = count_status("sl_hit")

    def avg(field, seq=closed):
        vals = [float(r.get(field) or 0.0) for r in seq if r.get(field) is not None]
        return float(mean(vals)) if vals else 0.0

    avg_fav = avg("max_favorable_r")
    avg_adv = avg("max_adverse_r")

    # expectancy: average realized R for closed signals (approx using first_outcome)
    realized = []
    for r in closed:
        fo = r.get("first_outcome")
        entry = r.get("entry_reference")
        sl = r.get("stop_loss")
        tp1 = r.get("tp1")
        tp2 = r.get("tp2")
        tp3 = r.get("tp3")
        if not entry or not sl:
            continue
        try:
            R = abs(float(entry) - float(sl))
            if R <= 0:
                continue
        except Exception:
            continue
        if fo == "tp1":
            realized.append(abs((float(entry) - float(tp1)) / R) if r.get("side") == "sell" else abs((float(tp1) - float(entry)) / R))
        elif fo == "tp2":
            realized.append(abs((float(entry) - float(tp2)) / R) if r.get("side") == "sell" else abs((float(tp2) - float(entry)) / R))
        elif fo == "tp3":
            realized.append(abs((float(entry) - float(tp3)) / R) if r.get("side") == "sell" else abs((float(tp3) - float(entry)) / R))
        elif fo == "sl":
            realized.append(-1.0)
        else:
            # unknown/expired
            continue

    expectancy = float(mean(realized)) if realized else 0.0

    # grouping based bests
    def best_by(field: str):
        groups = {}
        for r in closed:
            k = r.get(field) or "<none>"
            groups.setdefault(k, []).append(r)
        best = None
        best_val = -math.inf
        for k, g in groups.items():
            vals = []
            for r in g:
                fo = r.get("first_outcome")
                if fo in ("tp1", "tp2", "tp3"):
                    vals.append(1)
                elif fo == "sl":
                    vals.append(-1)
            score = mean(vals) if vals else 0.0
            if score > best_val:
                best_val = score
                best = k
        return best

    metrics = {
        "timestamp_utc": now_iso(),
        "total_signals": total,
        "open_signals": len(open_signals),
        "closed_signals": len(closed),
        "tp1_hits": tp1,
        "tp2_hits": tp2,
        "tp3_hits": tp3,
        "sl_hits": sl,
        "tp1_hit_rate": (tp1 / len(closed) * 100.0) if closed else 0.0,
        "tp2_hit_rate": (tp2 / len(closed) * 100.0) if closed else 0.0,
        "tp3_hit_rate": (tp3 / len(closed) * 100.0) if closed else 0.0,
        "sl_hit_rate": (sl / len(closed) * 100.0) if closed else 0.0,
        "average_max_favorable_r": avg_fav,
        "average_max_adverse_r": avg_adv,
        "expectancy_r": expectancy,
        "best_setup_grade": best_by("grade"),
        "best_session": best_by("session"),
        "best_macro_state": best_by("macro_state"),
        "best_sentiment_state": best_by("sentiment_state"),
    }
    return metrics


def render_md(metrics: dict) -> str:
    lines = []
    lines.append(f"# Paper Performance Report — {metrics.get('timestamp_utc')}")
    lines.append("")
    lines.append(f"- **Total signals**: {metrics.get('total_signals')}")
    lines.append(f"- **Open signals**: {metrics.get('open_signals')}")
    lines.append(f"- **Closed signals**: {metrics.get('closed_signals')}")
    lines.append("")
    lines.append(f"- **TP1 hit rate**: {metrics.get('tp1_hit_rate'):.1f}%")
    lines.append(f"- **TP2 hit rate**: {metrics.get('tp2_hit_rate'):.1f}%")
    lines.append(f"- **TP3 hit rate**: {metrics.get('tp3_hit_rate'):.1f}%")
    lines.append(f"- **SL hit rate**: {metrics.get('sl_hit_rate'):.1f}%")
    lines.append("")
    lines.append(f"- **Average max favorable R**: {metrics.get('average_max_favorable_r'):.3f}")
    lines.append(f"- **Average max adverse R**: {metrics.get('average_max_adverse_r'):.3f}")
    lines.append(f"- **Expectancy (R)**: {metrics.get('expectancy_r'):.3f}")
    lines.append("")
    lines.append(f"- **Best setup grade**: {metrics.get('best_setup_grade')}")
    lines.append(f"- **Best session**: {metrics.get('best_session')}")
    lines.append(f"- **Best macro state**: {metrics.get('best_macro_state')}")
    lines.append(f"- **Best sentiment state**: {metrics.get('best_sentiment_state')}")
    return "\n".join(lines)


def main():
    rows = read_outcomes()
    metrics = compute_metrics(rows)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    OUT_MD.write_text(render_md(metrics), encoding="utf-8")
    print("wrote report", OUT_JSON, OUT_MD)


if __name__ == "__main__":
    main()
