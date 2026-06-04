"""Read the paper-trade journal and report filter / regime lift.

This is the operational validation report the honest-eval roadmap calls
for: collect 30+ live paper trades with regime + filter tags, then ask:

  - does ``filter_verdict='allow'`` outperform ``'block'``?
  - is realised R aligned with expected R, or is execution drift killing
    the edge?
  - which regime buckets concentrate winners vs losers?

If allow-only PF >> block-only PF over n>=30 trades AND drift is small,
we promote ``GOLD_MACRO_FILTER`` from ``soft`` to ``hard``.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path


def _to_float(s: str) -> float:
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _stats(rs: list[float]) -> dict:
    if not rs:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "avg_r": 0.0, "total_r": 0.0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gw = sum(wins)
    gl = -sum(losses)
    pf = gw / gl if gl > 0 else float("inf")
    return {
        "n": len(rs),
        "wr": len(wins) / len(rs),
        "pf": pf,
        "avg_r": statistics.fmean(rs),
        "total_r": sum(rs),
    }


def _fmt(s: dict) -> str:
    pf = f"{s['pf']:.3f}" if s["pf"] != float("inf") else "  inf"
    return (
        f"n={s['n']:>4d}  wr={s['wr']:>5.1%}  pf={pf:>6}  "
        f"avg_r={s['avg_r']:>+6.3f}  total_r={s['total_r']:>+7.2f}"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--journal", type=Path,
                   default=Path("logs/trade_journal.csv"))
    p.add_argument("--min-bucket", type=int, default=5,
                   help="Min trades for a regime bucket to be reported.")
    args = p.parse_args()

    if not args.journal.exists():
        print(f"no journal yet at {args.journal}.  "
              "Run scripts/update_journal.py after agent-cycle to populate.")
        return 0

    with args.journal.open("r", newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("journal is empty")
        return 0

    realised = [_to_float(r["realised_r"]) for r in rows]
    expected = [_to_float(r["expected_r"]) for r in rows]
    drift = [_to_float(r["drift_r"]) for r in rows]
    n = len(rows)

    print(f"=== Paper-trade journal: {args.journal} ===")
    print(f"trades: {n}")
    print(f"date range: {rows[0]['opened_at']} -> {rows[-1]['closed_at']}")
    print()
    print("Overall:")
    print(f"  expected: {_fmt(_stats(expected))}")
    print(f"  realised: {_fmt(_stats(realised))}")
    if drift:
        avg_drift = statistics.fmean(drift)
        sd_drift = statistics.stdev(drift) if len(drift) >= 2 else 0.0
        print(f"  drift_avg_r={avg_drift:+.4f}  stdev={sd_drift:.4f}")
        if abs(avg_drift) >= 0.05:
            print(f"  WARNING: |drift|>=0.05R — simulator vs live disagree")

    # Filter-bucket lift.
    by_verdict: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_verdict[r.get("filter_verdict", "n/a")].append(_to_float(r["realised_r"]))
    print()
    print("By macro-filter verdict (would-have-been if filter was on):")
    for verdict, rs in sorted(by_verdict.items()):
        print(f"  {verdict:<22} {_fmt(_stats(rs))}")

    if "allow" in by_verdict and "block" in by_verdict:
        s_a = _stats(by_verdict["allow"] + by_verdict.get("allow_with_warning", []))
        s_b = _stats(by_verdict["block"])
        print()
        if s_a["n"] >= 5 and s_b["n"] >= 5:
            lift = (s_a["avg_r"] - s_b["avg_r"])
            print(
                f"FILTER LIFT (allow+warn vs block): "
                f"avg_r delta = {lift:+.3f}R   "
                f"({'positive' if lift > 0 else 'negative or zero'} signal)"
            )
            if n >= 30 and lift >= 0.10:
                print("  -> n>=30 AND lift>=0.10R — consider promoting filter to HARD mode.")
            elif n < 30:
                print(f"  -> need >=30 trades for confidence; currently {n}.")
        else:
            print(f"  not enough data per bucket yet (allow={s_a['n']}, block={s_b['n']})")

    # Regime-by-regime breakdown.
    for col in (
        "regime_vol_pct", "regime_trend", "regime_compression",
        "regime_spread", "regime_macro_real10y", "regime_macro_dxy",
        "regime_macro_vix",
    ):
        bucket: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            v = r.get(col, "")
            if v:
                bucket[v].append(_to_float(r["realised_r"]))
        if not bucket:
            continue
        bucket_filtered = {k: v for k, v in bucket.items() if len(v) >= args.min_bucket}
        if not bucket_filtered:
            continue
        print()
        print(f"By {col}:")
        for k, rs in sorted(bucket_filtered.items()):
            print(f"  {k:<22} {_fmt(_stats(rs))}")

    # Exit-reason mix.
    print()
    counts = Counter(r.get("exit_reason", "unknown") for r in rows)
    print(f"Exit-reason mix: {dict(counts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
