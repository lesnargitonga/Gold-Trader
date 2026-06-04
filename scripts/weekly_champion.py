"""Weekly champion selector — runs Sundays via cron.

Idea
----
For every self-contained strategy family we have, run a holdout-eval on the
last N days of bars, gate on quality (min trades, min PF, p-value), and write
the ranked survivors to ``config/champion.json``.  The next agent-cycle
reads that file and trades only the surviving families for the upcoming week.

This automates the workflow described in real-world feedback: re-search
across many parameter combinations every Sunday, pick what worked best in the
last 30 days, run that for one more week, repeat.

Usage (auto-fetch — preferred):
    PYTHONPATH=src .venv/bin/python scripts/weekly_champion.py --days 30

Usage (explicit CSV — for reproducible reruns):
    PYTHONPATH=src .venv/bin/python scripts/weekly_champion.py \
        --csv data/xauusd_full_15m.csv

Cron line (Sundays 22:00 UTC):
    0 22 * * 0 cd /home/lesnar/Documents/Gold\\ trader \\
        && PYTHONPATH=src .venv/bin/python scripts/weekly_champion.py --days 30 \\
            >> logs/champion.log 2>&1

Note: nothing in the live agent depends on this file existing — if missing,
``run_agent_cycle.sh`` falls back to its built-in default families list.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

# Make `src/` importable when run as a plain script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from gold_trader.data import (  # noqa: E402
    download_dukascopy_bars,
    load_bars_from_csv,
    resample_bars,
    write_bars_to_csv,
)
from gold_trader.models import BacktestConfig  # noqa: E402
from gold_trader.research.family_grids import (  # noqa: E402
    MACRO_FAMILIES,
    SELF_CONTAINED_FAMILIES,
    family_spec,
    family_spec_with_macro,
)
from gold_trader.research.holdout import run_holdout_evaluation  # noqa: E402


def _params_to_dict(params: object) -> dict:
    if is_dataclass(params):
        return asdict(params)
    if hasattr(params, "__dict__"):
        return dict(vars(params))
    return {"value": str(params)}


def evaluate_family(
    family: str,
    bars,
    *,
    holdout_fraction: float,
    n_permutations: int,
    workers: int,
    macro_frame=None,
) -> dict | None:
    """Run holdout-eval on one family.  Returns a dict candidate or None on failure."""
    try:
        if family in MACRO_FAMILIES:
            if macro_frame is None:
                return {
                    "family": family,
                    "error": "macro_frame_required",
                    "elapsed_seconds": 0.0,
                }
            spec = family_spec_with_macro(family, macro_frame)
            # Macro factory closes over a non-picklable MacroFrame; force
            # single-process execution to avoid ProcessPool serialization.
            workers = 1
        else:
            spec = family_spec(family)
    except KeyError:
        return None

    started = time.time()
    try:
        result = run_holdout_evaluation(
            bars=bars,
            param_grid=spec.grid,
            strategy_factory=spec.factory,
            config=BacktestConfig(commission_per_trade=10.0),
            holdout_fraction=holdout_fraction,
            n_permutations=n_permutations,
            family=family,
            family_name=family,
            n_workers=workers,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "family": family,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.time() - started, 1),
        }
    elapsed = round(time.time() - started, 1)

    summary = result.holdout_summary
    pf = summary.profit_factor
    if pf == float("inf"):
        pf = 999.0
    return {
        "family": family,
        "best_params": _params_to_dict(result.best_params),
        "train_bars": result.train_bars,
        "holdout_bars": result.holdout_bars,
        "train_pf": round(float(result.train_pf), 4),
        "holdout_pf": round(float(pf), 4),
        "holdout_trades": int(summary.total_trades),
        "holdout_total_return": round(float(summary.total_return), 6),
        "holdout_max_dd": round(float(summary.max_drawdown), 6),
        "holdout_win_rate": round(float(summary.win_rate), 4),
        "permutation_p": round(float(result.holdout_permutation.p_value), 4),
        "wf_positive_ratio": round(float(result.true_walk_forward.positive_window_ratio), 4),
        "wf_avg_pf": round(float(result.true_walk_forward.average_profit_factor), 4),
        "wf_windows": int(result.true_walk_forward.window_count),
        "verdict": result.verdict,
        "elapsed_seconds": elapsed,
    }


def score_candidate(c: dict) -> float:
    """Composite score: holdout PF * sqrt(trades) damped by p-value."""
    pf = float(c.get("holdout_pf", 0.0) or 0.0)
    n = max(int(c.get("holdout_trades", 0) or 0), 0)
    p = float(c.get("permutation_p", 1.0) or 1.0)
    if pf <= 0 or n == 0:
        return 0.0
    # sqrt(n) reward for sample size; (1-p) discount for noisy edges
    confidence = max(0.0, 1.0 - p)
    return pf * (n ** 0.5) * confidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Pick this week's strategy champions.")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--csv", help="Bar CSV (overrides auto-fetch). Mutually exclusive with --days.")
    src.add_argument("--days", type=int, default=30, help="Auto-fetch last N days from Dukascopy. Default 30.")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--timeframe-minutes", type=int, default=15, help="Bar timeframe for evaluation.")
    parser.add_argument("--output", default="config/champion.json", help="Where to write champion json.")
    parser.add_argument("--families", default="", help="Comma list to restrict; empty = all self-contained.")
    parser.add_argument("--exclude", default="", help="Comma list to exclude.")
    parser.add_argument("--top", type=int, default=5, help="Keep top-K survivors by score.")
    parser.add_argument("--holdout-fraction", type=float, default=1 / 3,
                        help="Fraction reserved for holdout. 30 days * 1/3 ≈ 10 days OOS.")
    parser.add_argument("--n-permutations", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--min-holdout-trades", type=int, default=8)
    parser.add_argument("--min-holdout-pf", type=float, default=1.10)
    parser.add_argument("--max-p-value", type=float, default=0.30)
    args = parser.parse_args()

    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            print(f"ERROR: csv not found: {csv_path}", file=sys.stderr)
            return 2
        bars = load_bars_from_csv(str(csv_path))
        source_label = str(csv_path)
        print(f"Loaded {len(bars)} bars from {csv_path}")
    else:
        from datetime import date, timedelta
        end_date = date.today()
        start_date = end_date - timedelta(days=max(args.days, 1) - 1)
        cache_dir = _REPO_ROOT / "data" / "champion_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        csv_path = cache_dir / f"{args.symbol.lower()}_{start_date}_{end_date}_{args.timeframe_minutes}m.csv"
        if csv_path.exists():
            print(f"Using cached fetch: {csv_path}")
            bars = load_bars_from_csv(str(csv_path))
        else:
            print(f"Auto-fetching {args.symbol} {start_date}..{end_date} 1m from Dukascopy ...")
            base_bars = download_dukascopy_bars(
                symbol=args.symbol,
                start_date=start_date,
                end_date=end_date,
                interval_minutes=1,
                max_workers=2,
            )
            print(f"  fetched {len(base_bars)} 1m bars")
            bars = (
                base_bars if args.timeframe_minutes == 1
                else resample_bars(base_bars, args.timeframe_minutes)
            )
            write_bars_to_csv(bars, csv_path)
            print(f"  resampled to {args.timeframe_minutes}m -> {len(bars)} bars  cached at {csv_path}")
        source_label = str(csv_path)

    if args.families.strip():
        family_list = [f.strip() for f in args.families.split(",") if f.strip()]
    else:
        # Default = self-contained families + the validated macro survivor.
        family_list = sorted(SELF_CONTAINED_FAMILIES.keys())
        family_list.append("timed_horizon_macro_regime")
    if args.exclude.strip():
        excluded = {f.strip() for f in args.exclude.split(",") if f.strip()}
        family_list = [f for f in family_list if f not in excluded]

    # Macro frame: load once, reuse across macro-family evaluations.
    _macro_frame = None
    if any(f in MACRO_FAMILIES for f in family_list):
        try:
            from gold_trader.data.macro import load_macro_frame
            _macro_frame = load_macro_frame(_REPO_ROOT / "data" / "macro")
            if not _macro_frame.names():
                _macro_frame = None
        except Exception as _exc:  # noqa: BLE001
            print(f"  WARN: macro frame unavailable ({_exc}); macro families will be skipped")
            _macro_frame = None

    print(f"Evaluating {len(family_list)} families: {family_list}")

    candidates: list[dict] = []
    started = time.time()
    for i, family in enumerate(family_list, 1):
        print(f"[{i}/{len(family_list)}] {family} ...", flush=True)
        cand = evaluate_family(
            family, bars,
            holdout_fraction=args.holdout_fraction,
            n_permutations=args.n_permutations,
            workers=args.workers,
            macro_frame=_macro_frame,
        )
        if cand is None:
            print(f"  skipped (no spec)", flush=True)
            continue
        if "error" in cand:
            print(f"  ERROR: {cand['error']} (elapsed={cand['elapsed_seconds']}s)", flush=True)
            candidates.append(cand)
            continue
        candidates.append(cand)
        print(
            f"  pf={cand['holdout_pf']} trades={cand['holdout_trades']} "
            f"p={cand['permutation_p']} verdict={cand['verdict']} "
            f"({cand['elapsed_seconds']}s)",
            flush=True,
        )

    total_elapsed = round(time.time() - started, 1)

    survivors: list[dict] = []
    for c in candidates:
        if "error" in c:
            continue
        if c["holdout_trades"] < args.min_holdout_trades:
            continue
        if c["holdout_pf"] < args.min_holdout_pf:
            continue
        if c["permutation_p"] > args.max_p_value:
            continue
        c2 = dict(c)
        c2["score"] = round(score_candidate(c), 4)
        survivors.append(c2)

    survivors.sort(key=lambda c: c["score"], reverse=True)
    top = survivors[: args.top]

    active_families_csv = ",".join(c["family"] for c in top) if top else ""

    payload = {
        "selected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "csv_path": source_label,
        "csv_bars": len(bars),
        "holdout_fraction": args.holdout_fraction,
        "elapsed_seconds": total_elapsed,
        "gates": {
            "min_holdout_trades": args.min_holdout_trades,
            "min_holdout_pf": args.min_holdout_pf,
            "max_p_value": args.max_p_value,
        },
        "all_results": candidates,
        "survivors": survivors,
        "champions": top,
        "active_families_csv": active_families_csv,
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nWrote {out}")
    print(f"survivors={len(survivors)}  top={len(top)}  active_families_csv={active_families_csv!r}")
    print(f"total_elapsed={total_elapsed}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
