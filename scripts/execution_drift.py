"""Analyse paper-trading execution drift.

Reads a paper_state.json (or live_state.json if structurally compatible)
and reports for each closed trade::

    expected_r   = (target - entry) / (entry - stop)   * direction
    realised_r   = (exit_price - entry) / (entry - stop) * direction
    drift_r      = realised_r - expected_r_at_outcome

Rolls up to:
    - expected vs realised mean R
    - expected vs realised win rate
    - drift histogram
    - exit-reason mix
    - per-regime breakdown (if a macro cache is present)

This is the operational counterpart to holdout-evaluation: it exposes
whether the *live* engine matches the simulator on the same rules.

For thin edges (PF~1.2), a 4-5% drift between expected and realised R
is the difference between a working system and a money-loser.  This
script makes that drift visible so we can act on it instead of trusting
the backtest blindly.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def _r_value(side: str, entry: float, stop: float, price: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    direction = 1.0 if side.lower().endswith("long") or side.lower() == "buy" else -1.0
    return (price - entry) * direction / risk


def analyse(state_path: Path) -> dict:
    with state_path.open("r") as f:
        data = json.load(f)
    closed = data.get("closed_positions", [])
    if not closed:
        return {"n": 0, "warning": "no closed positions"}

    rows = []
    exit_reason_counts: Counter[str] = Counter()
    expected_rs: list[float] = []
    realised_rs: list[float] = []
    drifts: list[float] = []
    wins_expected = 0
    wins_realised = 0
    by_exit: dict[str, list[float]] = defaultdict(list)

    for pos in closed:
        side = str(pos.get("side", "long"))
        entry = float(pos["entry"])
        stop = float(pos["stop"])
        target = float(pos["target"])
        exit_price = pos.get("closed_price")
        exit_reason = pos.get("exit_reason") or "unknown"
        if exit_price is None:
            continue
        exit_price = float(exit_price)

        # Expected R is +risk_reward for target hit, -1.0 for stop hit, ~0 for time exit.
        expected_r_target = _r_value(side, entry, stop, target)
        # Map outcome: realised R from actual fill.
        realised_r = _r_value(side, entry, stop, exit_price)
        # The "expected at outcome" is target-RR if target hit, -1 if stop hit,
        # realised price otherwise (time exits had no expectation).
        if exit_reason == "target":
            expected_at_outcome = expected_r_target
            wins_expected += 1
        elif exit_reason == "stop":
            expected_at_outcome = -1.0
        else:
            expected_at_outcome = realised_r  # no clean expectation

        if realised_r > 0:
            wins_realised += 1

        drift = realised_r - expected_at_outcome
        rows.append({
            "opened_at": pos.get("opened_at"),
            "family": pos.get("family"),
            "tf": pos.get("timeframe_minutes"),
            "side": side,
            "entry": entry,
            "stop": stop,
            "target": target,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "expected_r": expected_at_outcome,
            "realised_r": realised_r,
            "drift_r": drift,
        })
        exit_reason_counts[exit_reason] += 1
        expected_rs.append(expected_at_outcome)
        realised_rs.append(realised_r)
        drifts.append(drift)
        by_exit[exit_reason].append(drift)

    n = len(rows)
    summary: dict = {
        "n": n,
        "expected_avg_r": round(statistics.fmean(expected_rs), 4) if expected_rs else 0.0,
        "realised_avg_r": round(statistics.fmean(realised_rs), 4) if realised_rs else 0.0,
        "drift_avg_r": round(statistics.fmean(drifts), 4) if drifts else 0.0,
        "drift_stdev_r": (
            round(statistics.stdev(drifts), 4) if len(drifts) >= 2 else 0.0
        ),
        "expected_win_rate": round(wins_expected / n, 3) if n else 0.0,
        "realised_win_rate": round(wins_realised / n, 3) if n else 0.0,
        "exit_reason_mix": dict(exit_reason_counts),
        "drift_by_exit_reason": {
            k: round(statistics.fmean(v), 4) for k, v in by_exit.items() if v
        },
    }
    return {"summary": summary, "trades": rows}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("state_path", type=Path)
    p.add_argument("--csv", type=Path, default=None,
                   help="Optional CSV dump of per-trade rows.")
    p.add_argument("--json", type=Path, default=None,
                   help="Optional JSON dump of full report.")
    args = p.parse_args(argv)

    if not args.state_path.exists():
        print(f"error: {args.state_path} not found", file=sys.stderr)
        return 2

    report = analyse(args.state_path)
    summary = report.get("summary", report)

    print(f"=== execution drift report: {args.state_path} ===")
    if summary.get("n", 0) == 0:
        print(summary.get("warning", "no trades"))
        return 0

    print(f"closed_trades:      {summary['n']}")
    print(f"expected_avg_r:     {summary['expected_avg_r']:+.4f}")
    print(f"realised_avg_r:     {summary['realised_avg_r']:+.4f}")
    print(f"drift_avg_r:        {summary['drift_avg_r']:+.4f}  "
          f"(stdev {summary['drift_stdev_r']:.4f})")
    print(f"expected_win_rate:  {summary['expected_win_rate']:.1%}")
    print(f"realised_win_rate:  {summary['realised_win_rate']:.1%}")
    print(f"exit_reason_mix:    {summary['exit_reason_mix']}")
    print(f"drift_by_exit:      {summary['drift_by_exit_reason']}")

    drift = summary["drift_avg_r"]
    if abs(drift) >= 0.05:
        print()
        print(f"⚠  drift |R| >= 0.05 — simulator and live execution disagree by "
              f"~{drift:+.2%} of risk per trade.  Investigate before scaling.")
    elif abs(drift) >= 0.02:
        print()
        print(f"note: drift {drift:+.4f}R is small but non-zero — monitor.")

    if args.csv is not None:
        import csv
        with args.csv.open("w", newline="") as f:
            if report["trades"]:
                w = csv.DictWriter(f, fieldnames=list(report["trades"][0].keys()))
                w.writeheader()
                w.writerows(report["trades"])
        print(f"wrote {args.csv}")

    if args.json is not None:
        args.json.write_text(json.dumps(report, indent=2, default=str))
        print(f"wrote {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
