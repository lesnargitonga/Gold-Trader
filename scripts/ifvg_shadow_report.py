#!/usr/bin/env python3
"""Summarize IFVG assistant shadow setups.

Outcome fields are intentionally manual for now. After reviewing a setup, fill
``outcome_r`` in the CSV and rerun this script to see score-bucket calibration.
"""
from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path


def _bucket(score: float) -> str:
    if score >= 80:
        return "80-100 valid"
    if score >= 65:
        return "65-79 alert"
    return "below 65"


def _stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0, "win_rate": 0.0, "avg_r": 0.0, "total_r": 0.0}
    wins = [v for v in values if v > 0]
    return {
        "n": len(values),
        "win_rate": len(wins) / len(values),
        "avg_r": statistics.fmean(values),
        "total_r": sum(values),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize IFVG shadow setup outcomes.")
    parser.add_argument(
        "csv_path",
        nargs="?",
        default="data/agent_live_xauusd/ifvg_shadow_setups.csv",
        help="Path to ifvg_shadow_setups.csv",
    )
    args = parser.parse_args()
    path = Path(args.csv_path)
    if not path.exists():
        print(f"no shadow journal found: {path}")
        return 0

    rows = list(csv.DictReader(path.open("r", newline="", encoding="utf-8")))
    by_bucket: dict[str, list[float]] = defaultdict(list)
    by_side: dict[str, list[float]] = defaultdict(list)
    pending = 0
    for row in rows:
        score = float(row.get("score") or 0.0)
        raw = (row.get("outcome_r") or "").strip()
        if not raw:
            pending += 1
            continue
        try:
            r = float(raw)
        except ValueError:
            pending += 1
            continue
        by_bucket[_bucket(score)].append(r)
        by_side[row.get("side") or "unknown"].append(r)

    print(f"IFVG shadow setups: total={len(rows)} reviewed={len(rows) - pending} pending={pending}")
    print("By score bucket:")
    for name in ("80-100 valid", "65-79 alert", "below 65"):
        s = _stats(by_bucket[name])
        print(f"  {name}: n={s['n']} wr={s['win_rate']:.1%} avg_r={s['avg_r']:.2f} total_r={s['total_r']:.2f}")
    print("By side:")
    for name in sorted(by_side):
        s = _stats(by_side[name])
        print(f"  {name}: n={s['n']} wr={s['win_rate']:.1%} avg_r={s['avg_r']:.2f} total_r={s['total_r']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
