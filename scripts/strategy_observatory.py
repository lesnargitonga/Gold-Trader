"""Strategy observatory — multi-strategy diagnostic dump.

Runs every self-contained strategy family on a CSV at default params,
captures every TradeSignal each emits (with score, verdict, filter
breakdown for scored families) along with its trade outcome, then
produces three aggregated views:

1. **signal_log.csv** — one row per (strategy, signal): timestamp,
   side, score, verdict, pnl_r, pnl, exit_reason.
2. **per_strategy.csv** — overall rank of each family by total
   PF × √n.
3. **per_strategy_bucket.csv** — per-(strategy, score_bucket) PF /
   avg_R / win-rate.  Tells you *where* each strategy shines (which
   score range is its sweet spot).
4. **concurrence.csv** — for each timestamp bucket, count how many
   distinct strategies fire simultaneously, and the joint forward-R.
   Multi-strategy confluence as a meta-signal.

This is *not* a gate.  It's instrumentation.  Used to derive empirical
strategy rankings and confluence rules from data, not to filter trades
in production.

Usage::

    PYTHONPATH=src .venv/bin/python scripts/strategy_observatory.py \\
        data/xauusd_5y/xauusd_5y_15m.csv \\
        --output-dir reports/observatory/5y_15m
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gold_trader.data import load_bars_from_csv
from gold_trader.backtest.engine import run_backtest
from gold_trader.models import BacktestConfig
from gold_trader.research.family_grids import (
    SELF_CONTAINED_FAMILIES,
    family_spec,
)
from gold_trader.ensemble import (
    load_strategy_weights,
    strategy_weight,
    concurrence_multiplier,
)


_BUCKET_ORDER = ["unscored", "<40", "[40,55)", "[55,70)", "[70,85)", "[85,100]"]


def _bucket(score: float, scored: bool) -> str:
    # With the engine auto-attaching universal scoring, every signal
    # has a real 0..100 score regardless of strategy-internal scoring.
    # Bucket purely on the score value.
    if score <= 0:
        return "unscored"
    if score < 40: return "<40"
    if score < 55: return "[40,55)"
    if score < 70: return "[55,70)"
    if score < 85: return "[70,85)"
    return "[85,100]"


@dataclass
class SignalRow:
    strategy: str
    timestamp: str
    side: str
    score: float
    verdict_bucket: str
    pnl_r: float
    pnl: float
    bars_held: int
    exit_reason: str
    strategy_weight: float = 1.0
    concurrence: int = 1
    signal_strength: float = 0.0


def _is_scored_strategy(strategy_obj) -> bool:
    """True if family has a non-empty filters_enabled (scoring active)."""
    fe = getattr(strategy_obj, "filters_enabled", None)
    return bool(fe)


def _pf(rows: list[SignalRow]) -> float:
    pos = sum(r.pnl_r for r in rows if r.pnl_r > 0)
    neg = sum(r.pnl_r for r in rows if r.pnl_r < 0)
    if neg == 0:
        return float("inf") if pos > 0 else 0.0
    return pos / abs(neg)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("csv_path")
    p.add_argument("--output-dir", default="reports/observatory")
    p.add_argument("--skip", default="", help="comma-list of families to skip")
    p.add_argument("--max-combos", type=int, default=1,
                   help="per-family grid combos to evaluate (1=default-params; "
                        "set higher to sweep — combos are evenly subsampled "
                        "across the family's grid)")
    p.add_argument("--dedup-by-strategy", action="store_true", default=True,
                   help="de-duplicate (timestamp, strategy_name) before "
                        "computing concurrence — required when max-combos>1 "
                        "so a single strategy firing from many param combos "
                        "counts as ONE concurrent strategy, not N")
    args = p.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    bars = load_bars_from_csv(args.csv_path)
    print(f"loaded {len(bars)} bars from {args.csv_path}")

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    families = [f for f in SELF_CONTAINED_FAMILIES if f not in skip]
    print(f"running {len(families)} families (skipping {sorted(skip)}) "
          f"max_combos={args.max_combos}")

    cfg = BacktestConfig()
    rows: list[SignalRow] = []
    fam_summary: dict[str, dict] = {}

    weights = load_strategy_weights()
    if weights:
        print(f"loaded {len(weights)} strategy weights from "
              "data/strategy_weights.json")
    else:
        print("no strategy_weights.json yet — using weight=1.0 for all "
              "(run scripts/compute_strategy_weights.py after this)")

    for fam in families:
        try:
            spec = family_spec(fam)
            grid = list(spec.grid)
            # Even-stride subsample of the family's grid (always include idx 0)
            n_combos = min(args.max_combos, len(grid))
            if n_combos <= 1:
                combos = [grid[0]]
            else:
                stride = max(1, len(grid) // n_combos)
                combos = [grid[i] for i in range(0, len(grid), stride)][:n_combos]
            scored = False
            fam_rows: list[SignalRow] = []
            seen_ts_side: set[tuple[str, str]] = set()
            for params in combos:
                strategy = spec.factory(params)
                scored = scored or _is_scored_strategy(strategy)
                r = run_backtest(bars, strategy, cfg)
                for tr in r.trades:
                    ts_iso = tr.entry_time.isoformat()
                    if args.dedup_by_strategy:
                        # First (ts, side) for this strategy wins; later combos
                        # firing same setup are dropped so concurrence counts
                        # remain honest.
                        key = (ts_iso, tr.side.name)
                        if key in seen_ts_side:
                            continue
                        seen_ts_side.add(key)
                    row = SignalRow(
                        strategy=fam,
                        timestamp=ts_iso,
                        side=tr.side.name,
                        score=float(tr.score),
                        verdict_bucket=_bucket(tr.score, scored),
                        pnl_r=tr.pnl_r,
                        pnl=tr.pnl,
                        bars_held=tr.bars_held,
                        exit_reason=tr.exit_reason,
                    )
                    rows.append(row)
                    fam_rows.append(row)
            n = len(fam_rows)
            wins = sum(1 for x in fam_rows if x.pnl_r > 0)
            avg_r = sum(x.pnl_r for x in fam_rows) / n if n else 0.0
            fam_summary[fam] = {
                "scored": scored,
                "n": n,
                "wins": wins,
                "win_rate": (wins / n * 100) if n else 0.0,
                "avg_r": avg_r,
                "pf": _pf(fam_rows),
                "score_pf_sqrt_n": _pf(fam_rows) * math.sqrt(n) if n else 0.0,
            }
            print(f"  {fam:30s}  combos={len(combos):>3d}  n={n:>5d}  "
                  f"PF={fam_summary[fam]['pf']:>5.2f}  avgR={avg_r:+.3f}  "
                  f"scored={scored}")
        except Exception as e:
            print(f"  {fam:30s}  SKIPPED ({type(e).__name__}: {e})")
            fam_summary[fam] = {"error": str(e)}

    # ------ enrich rows with concurrence & signal_strength
    by_ts: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        by_ts[r.timestamp].add(r.strategy)
    for r in rows:
        c = len(by_ts[r.timestamp])
        sw = strategy_weight(r.strategy, weights)
        r.concurrence = c
        r.strategy_weight = sw
        r.signal_strength = max(0.0, r.score) * sw * concurrence_multiplier(c)

    # ------ signal_log.csv
    sig_path = out / "signal_log.csv"
    with sig_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(SignalRow.__dataclass_fields__))
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))
    print(f"\nwrote {len(rows)} signals → {sig_path}")

    # ------ per_strategy.csv (ranked)
    rank_path = out / "per_strategy.csv"
    ranked = sorted(
        ((fam, s) for fam, s in fam_summary.items() if "error" not in s),
        key=lambda kv: kv[1]["score_pf_sqrt_n"],
        reverse=True,
    )
    with rank_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "strategy", "scored", "n", "wins",
                    "win_rate_pct", "avg_r", "pf", "rank_score_pf_sqrt_n"])
        for i, (fam, s) in enumerate(ranked, 1):
            w.writerow([i, fam, s["scored"], s["n"], s["wins"],
                        f"{s['win_rate']:.1f}", f"{s['avg_r']:+.3f}",
                        f"{s['pf']:.3f}", f"{s['score_pf_sqrt_n']:.2f}"])
    print(f"wrote {rank_path}")

    # ------ per_strategy_bucket.csv
    bucket_path = out / "per_strategy_bucket.csv"
    by_fam_bucket: dict[tuple[str, str], list[SignalRow]] = defaultdict(list)
    for r in rows:
        by_fam_bucket[(r.strategy, r.verdict_bucket)].append(r)
    with bucket_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "bucket", "n", "wins", "win_rate_pct",
                    "avg_r", "pf"])
        for (fam, bk) in sorted(by_fam_bucket,
                                 key=lambda x: (x[0], _BUCKET_ORDER.index(x[1])
                                                if x[1] in _BUCKET_ORDER else 99)):
            br = by_fam_bucket[(fam, bk)]
            n = len(br)
            wins = sum(1 for x in br if x.pnl_r > 0)
            avg = sum(x.pnl_r for x in br) / n if n else 0.0
            w.writerow([fam, bk, n, wins, f"{wins/n*100:.1f}" if n else "0.0",
                        f"{avg:+.3f}", f"{_pf(br):.3f}"])
    print(f"wrote {bucket_path}")

    # ------ concurrence.csv — group signals by entry timestamp; if
    # multiple strategies fire on the same bar, that's a confluence event.
    conc_path = out / "concurrence.csv"
    by_ts: dict[str, list[SignalRow]] = defaultdict(list)
    for r in rows:
        by_ts[r.timestamp].append(r)
    # Bucket by concurrence count → aggregate forward-R
    by_count: dict[int, list[SignalRow]] = defaultdict(list)
    for ts, rs in by_ts.items():
        n_distinct = len({x.strategy for x in rs})
        for x in rs:
            by_count[n_distinct].append(x)
    with conc_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["concurrent_strategies_count", "n_signals",
                    "avg_r", "pf", "win_rate_pct"])
        for k in sorted(by_count):
            br = by_count[k]
            n = len(br)
            wins = sum(1 for x in br if x.pnl_r > 0)
            avg = sum(x.pnl_r for x in br) / n if n else 0.0
            w.writerow([k, n, f"{avg:+.3f}", f"{_pf(br):.3f}",
                        f"{wins/n*100:.1f}" if n else "0.0"])
    print(f"wrote {conc_path}")

    # Print concurrence summary inline
    print("\nconcurrence summary (how many strategies fire same bar → joint PF):")
    print(f"  {'concurrent':>10s} {'n':>7s} {'avg_R':>8s} {'PF':>6s} {'win%':>6s}")
    for k in sorted(by_count):
        br = by_count[k]
        n = len(br)
        wins = sum(1 for x in br if x.pnl_r > 0)
        avg = sum(x.pnl_r for x in br) / n if n else 0.0
        print(f"  {k:>10d} {n:>7d} {avg:>+8.3f} {_pf(br):>6.2f} "
              f"{wins/n*100 if n else 0:>5.1f}%")

    return 0


if __name__ == "__main__":
    sys.exit(main())
