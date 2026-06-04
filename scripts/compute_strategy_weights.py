"""Compute strategy_weights.json from observatory output.

Reads ``reports/observatory/<run>/per_strategy.csv`` and derives a
weight in [0..1] per strategy:

    raw    = PF × sqrt(n)             (with PF clipped to [0, 5])
    weight = raw / max(raw across strategies)

Strategies with n < min_n get weight=0 (insufficient sample).

The weight is what ``ensemble.signal_strength`` uses to scale the
inside score live.  Walk-forward note: this script should be run on
out-of-sample data only (or on a rolling-window split of in-sample),
otherwise the meta-weights overfit the training period.

Usage::

    PYTHONPATH=src .venv/bin/python scripts/compute_strategy_weights.py \\
        reports/observatory/5y_15m/per_strategy.csv \\
        --output data/strategy_weights.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("per_strategy_csv")
    p.add_argument("--output", default="data/strategy_weights.json")
    p.add_argument("--min-n", type=int, default=20,
                   help="strategies with n<min_n get weight=0")
    p.add_argument("--pf-cap", type=float, default=5.0)
    args = p.parse_args()

    raw: dict[str, dict] = {}
    with open(args.per_strategy_csv) as f:
        for row in csv.DictReader(f):
            try:
                n = int(row["n"])
                pf = float(row["pf"])
            except (KeyError, ValueError):
                continue
            pf_clipped = max(0.0, min(args.pf_cap, pf))
            score = pf_clipped * math.sqrt(n) if n >= args.min_n else 0.0
            raw[row["strategy"]] = {
                "n": n,
                "pf": pf,
                "raw_score": score,
            }

    if not raw:
        print(f"no rows parsed from {args.per_strategy_csv}", file=sys.stderr)
        return 1

    max_score = max(v["raw_score"] for v in raw.values())
    if max_score <= 0:
        print("all strategies below min-n; refusing to write zero weights",
              file=sys.stderr)
        return 1

    weights = {k: v["raw_score"] / max_score for k, v in raw.items()}

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(Path(args.per_strategy_csv).resolve()),
        "min_n": args.min_n,
        "pf_cap": args.pf_cap,
        "weights": weights,
        "raw": raw,
    }
    out.write_text(json.dumps(payload, indent=2))
    print(f"wrote {len(weights)} weights → {out}")
    print("top 5 by weight:")
    for name, w in sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:5]:
        r = raw[name]
        print(f"  {name:30s}  weight={w:.3f}  n={r['n']:>4d}  pf={r['pf']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
