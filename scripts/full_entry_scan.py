#!/usr/bin/env python3
"""Unbiased full-system entry scan — discovery only, no manipulation gates.

Walks configured timeframe CSVs and replays ``build_bundle_snapshot`` at a
configurable cadence (default M60). Every unique entry candidate emitted by
the research engine is recorded with full bundle context. Bundle accept/reject
is metadata only; simulation (optional) is labeled separately.

Outputs:
  logs/full_entry_scan_signals.csv
  logs/full_entry_scan_report.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics as stats
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.research import build_bundle_snapshot  # noqa: E402

# Families wired in research/state.py _entry_candidates (same as live bundle).
FULL_FAMILIES = (
    "inversion_fair_value_gap",
    "liquidity_sweep",
    "compression_breakout",
    "asian_range_breakout",
    "london_breakout",
    "trend_pullback",
    "ny_session_breakout",
    "momentum_burst",
    "timed_horizon_macro_regime",
)

LEVELS = REPO / "config" / "market_levels.json"
NEWS = REPO / "data" / "macro" / "news_calendar.csv"
OPENAI_CFG = REPO / "config" / "openai_research.json"
SHADOW = REPO / "logs" / "ifvg_shadow_journal.csv"

# High cap so snapshot discovery is not truncated by max_candidates ranking.
MAX_CANDIDATES_PER_SNAPSHOT = 500
MIN_BARS_PER_TF = 80

CONTRACT = 100.0
MIN_LOT = 0.01
SPREAD_RT = 0.70
DEFAULT_RISK_USD = 30.0

SIGNAL_FIELDS = (
    "time",
    "snapshot_time",
    "family",
    "tf",
    "side",
    "score",
    "regime_fit",
    "reason",
    "conflict",
    "entry",
    "stop",
    "target",
    "rr",
    "sl_dist",
    "ifvg_grade",
    "ifvg_tech_score",
    "ifvg_verdict",
    "htf_bias",
    "alignment",
    "oscillation",
    "decision_status",
    "decision_family",
    "decision_is_top",
    "decision_rationale",
    "warnings",
)


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _slice_bars(bars, end: datetime):
    return [b for b in bars if b.timestamp <= end]


def _ifvg_grade(candidate) -> str | None:
    if candidate.family != "inversion_fair_value_gap":
        return None
    details = candidate.details or {}
    grading = details.get("grading") or {}
    return str(grading.get("letter") or details.get("grade") or "?")


def _ifvg_tech_score(candidate) -> int | None:
    if candidate.family != "inversion_fair_value_gap":
        return None
    details = candidate.details or {}
    return int(details.get("score") or candidate.score or 0)


def collect_signals(
    all_bars: dict[int, list],
    *,
    start: datetime,
    end: datetime,
    macro,
    cadence_minutes: int,
) -> list[dict]:
    """Replay bundle snapshots; dedupe unique entry candidates across the window."""
    anchor_bars = all_bars.get(cadence_minutes) or all_bars.get(15) or []
    step_bars = [b for b in anchor_bars if start <= b.timestamp <= end]
    if not step_bars:
        return []

    seen: set[tuple] = set()
    signals: list[dict] = []

    for n, anchor in enumerate(step_bars):
        if n and n % 100 == 0:
            print(f"  ... snapshot {n}/{len(step_bars)}", flush=True)

        datasets = {
            tf: _slice_bars(bars, anchor.timestamp)
            for tf, bars in all_bars.items()
            if len(_slice_bars(bars, anchor.timestamp)) >= MIN_BARS_PER_TF
        }
        if len(datasets) < 2:
            continue

        snap = build_bundle_snapshot(
            datasets,
            families=FULL_FAMILIES,
            max_candidates=MAX_CANDIDATES_PER_SNAPSHOT,
            macro_frame=macro,
            market_levels_path=str(LEVELS),
            news_calendar_path=str(NEWS),
            shadow_journal_path=str(SHADOW),
            openai_research_config_path=str(OPENAI_CFG),
            openai_research_cache_path=str(REPO / "data/cache/openai_market_research.json"),
        )

        top = snap.entry_candidates[0] if snap.entry_candidates else None
        decision = snap.decision
        decision_rationale = " | ".join(decision.rationale) if decision.rationale else ""

        for cand in snap.entry_candidates:
            tf_bars = datasets.get(cand.timeframe_minutes) or []
            if not tf_bars:
                continue
            bar_ts = tf_bars[-1].timestamp
            key = (
                cand.family,
                cand.timeframe_minutes,
                cand.side.value,
                bar_ts.isoformat(),
                round(cand.reference_price, 2),
                round(cand.stop, 2),
                round(cand.target, 2),
            )
            if key in seen:
                continue
            seen.add(key)

            sl_dist = abs(cand.reference_price - cand.stop)
            rr = abs(cand.target - cand.reference_price) / sl_dist if sl_dist > 0 else 0.0

            signals.append({
                "time": bar_ts.isoformat(),
                "snapshot_time": anchor.timestamp.isoformat(),
                "family": cand.family,
                "tf": cand.timeframe_minutes,
                "side": cand.side.value,
                "score": cand.score,
                "regime_fit": cand.regime_fit,
                "reason": cand.reason,
                "conflict": cand.conflict or "",
                "entry": cand.reference_price,
                "stop": cand.stop,
                "target": cand.target,
                "rr": round(rr, 3),
                "sl_dist": round(sl_dist, 2),
                "ifvg_grade": _ifvg_grade(cand),
                "ifvg_tech_score": _ifvg_tech_score(cand),
                "ifvg_verdict": (cand.details or {}).get("verdict") if cand.family == "inversion_fair_value_gap" else None,
                "htf_bias": snap.higher_timeframe_bias,
                "alignment": snap.alignment_label,
                "oscillation": snap.oscillation_label,
                "decision_status": decision.status,
                "decision_family": decision.family or "",
                "decision_is_top": bool(
                    top is not None
                    and cand.family == top.family
                    and cand.timeframe_minutes == top.timeframe_minutes
                    and cand.side is top.side
                ),
                "decision_rationale": decision_rationale,
                "warnings": "; ".join(snap.warnings),
            })

    return signals


def _size_lots(entry: float, stop: float, risk_usd: float) -> float | None:
    sl = abs(entry - stop)
    if sl <= 0:
        return None
    lots = risk_usd / (sl * CONTRACT)
    return max(MIN_LOT, min(0.05, math.floor(lots / MIN_LOT) * MIN_LOT))


def _find_bar_idx(bars, ts: datetime) -> int:
    for i, b in enumerate(bars):
        if b.timestamp == ts:
            return i
    idx = 0
    for i, b in enumerate(bars):
        if b.timestamp <= ts:
            idx = i
        else:
            break
    return idx


def _simulate(bars, start_idx: int, *, side: str, entry: float, stop: float, target: float, max_bars: int):
    is_long = side == "long"
    end = min(len(bars), start_idx + 1 + max_bars)
    for j in range(start_idx + 1, end):
        bar = bars[j]
        if is_long:
            if bar.low <= stop:
                return "loss", stop, j
            if bar.high >= target:
                return "win", target, j
        else:
            if bar.high >= stop:
                return "loss", stop, j
            if bar.low <= target:
                return "win", target, j
    return "open", None, start_idx


def simulate_all(signals: list[dict], bars_by_tf: dict[int, list], *, risk_usd: float) -> list[dict]:
    """Honest fixed-risk PnL on engine entry/stop/target — not used to filter discovery."""
    rows = sorted(signals, key=lambda r: r["time"])
    executed: list[dict] = []
    busy_until: datetime | None = None

    for s in rows:
        ts = _parse_ts(s["time"])
        if busy_until and ts <= busy_until:
            continue
        tf = int(s["tf"])
        bars = bars_by_tf.get(tf) or []
        idx = _find_bar_idx(bars, ts)
        lots = _size_lots(s["entry"], s["stop"], risk_usd)
        if lots is None:
            continue
        horizon = {5: 576, 15: 384, 60: 168, 240: 96}.get(tf, 384)
        outcome, exit_px, exit_idx = _simulate(
            bars,
            idx,
            side=s["side"],
            entry=s["entry"],
            stop=s["stop"],
            target=s["target"],
            max_bars=horizon,
        )
        if outcome == "open":
            continue
        spread = SPREAD_RT * (lots / MIN_LOT)
        if outcome == "win":
            move = abs(exit_px - s["entry"])
            pnl = move * CONTRACT * lots - spread
        else:
            pnl = -abs(s["entry"] - s["stop"]) * CONTRACT * lots - spread
        busy_until = bars[exit_idx].timestamp
        executed.append({
            **s,
            "lots": lots,
            "outcome": outcome,
            "pnl_usd": round(pnl, 2),
            "exit_time": bars[exit_idx].timestamp.isoformat(),
        })
    return executed


def _summarize_pnl(trades: list[dict], key_fn) -> dict[str, dict]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        buckets[str(key_fn(t))].append(float(t["pnl_usd"]))
    out = {}
    for k, pnls in sorted(buckets.items()):
        w = sum(1 for p in pnls if p > 0)
        out[k] = {
            "trades": len(pnls),
            "wins": w,
            "losses": len(pnls) - w,
            "win_rate": round(w / len(pnls), 4) if pnls else 0,
            "net_pnl": round(sum(pnls), 2),
            "avg_pnl": round(stats.mean(pnls), 2) if pnls else 0,
        }
    return out


def _count_by(signals: list[dict], key: str) -> dict[str, int]:
    return dict(Counter(str(s.get(key) or "?") for s in signals))


def main() -> int:
    p = argparse.ArgumentParser(description="Unbiased full-system entry scan (no filter gates)")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--end", default="2026-05-29")
    p.add_argument("--data-dir", default=str(REPO / "data" / "agent_live_xauusd"))
    p.add_argument(
        "--cadence",
        type=int,
        default=60,
        help="Snapshot anchor TF in minutes (60=M60). Lower = more snapshots, slower but more complete.",
    )
    p.add_argument("--risk-usd", type=float, default=DEFAULT_RISK_USD, help="Fixed risk for optional simulation only")
    p.add_argument("--no-simulate", action="store_true", help="Skip honest PnL simulation block in report")
    args = p.parse_args()

    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=23, minutes=59)
    start_dt = end_dt - timedelta(days=args.days)
    data_dir = Path(args.data_dir)

    macro = load_macro_frame(REPO / "data" / "macro")
    if macro is not None and not macro.names():
        macro = None

    all_bars: dict[int, list] = {}
    loaded_tfs: list[int] = []
    for tf, fname in ((5, "5m"), (15, "15m"), (60, "60m"), (240, "240m")):
        path = data_dir / f"xauusd_{fname}.csv"
        if not path.exists():
            continue
        full = load_bars_from_csv(path)
        all_bars[tf] = [b for b in full if b.timestamp <= end_dt]
        loaded_tfs.append(tf)

    print(f"Loaded TFs: {loaded_tfs}", flush=True)
    print(f"Scan window: {start_dt.date()} → {end_dt.date()} (cadence={args.cadence}m)", flush=True)
    signals = collect_signals(
        all_bars,
        start=start_dt,
        end=end_dt,
        macro=macro,
        cadence_minutes=args.cadence,
    )
    print(f"Unique entry candidates (discovery): {len(signals)}", flush=True)

    sim_trades: list[dict] = []
    simulation_block: dict | None = None
    if not args.no_simulate and signals:
        sim_trades = simulate_all(signals, all_bars, risk_usd=args.risk_usd)
        simulation_block = {
            "label": "optional_honest_pnl_not_used_for_discovery",
            "one_position_at_a_time": True,
            "risk_usd_per_trade": args.risk_usd,
            "spread_rt_per_0_01_lot": SPREAD_RT,
            "uses_engine_entry_stop_target_unchanged": True,
            "trades": len(sim_trades),
            "net_pnl": round(sum(t["pnl_usd"] for t in sim_trades), 2),
            "by_family": _summarize_pnl(sim_trades, lambda t: t["family"]),
            "by_ifvg_grade": _summarize_pnl(
                [t for t in sim_trades if t.get("ifvg_grade")],
                lambda t: t["ifvg_grade"],
            ),
            "by_decision_status_at_signal": _summarize_pnl(sim_trades, lambda t: t["decision_status"]),
            "by_htf_bias": _summarize_pnl(sim_trades, lambda t: t["htf_bias"]),
            "by_alignment": _summarize_pnl(sim_trades, lambda t: t["alignment"]),
        }

    ifvg_signals = [s for s in signals if s["family"] == "inversion_fair_value_gap"]

    report = {
        "methodology": {
            "purpose": "unbiased_full_system_entry_discovery",
            "engine": "gold_trader.research.state.build_bundle_snapshot",
            "no_filters_applied": True,
            "explicitly_not_applied": [
                "grade filter (all IFVG A/B/C/D and scores included)",
                "min risk-reward or max stop distance filter",
                "quality mode or score threshold on discovery",
                "family cherry-picking (all FULL_FAMILIES from state.py)",
                "bundle accept/reject as entry filter (recorded as metadata only)",
                "TP/SL rewrite (engine reference_price/stop/target used as-is)",
            ],
            "engine_internal_limits_documented": [
                "IFVG setups with verdict=ignore are skipped inside state.py (engine behavior)",
                f"max_candidates={MAX_CANDIDATES_PER_SNAPSHOT} per snapshot to avoid rank truncation",
                f"min_bars_per_tf={MIN_BARS_PER_TF} for snapshot eligibility",
            ],
            "families": list(FULL_FAMILIES),
            "data_layers": {
                "timeframes_loaded": loaded_tfs,
                "multi_tf_bundle_analysis": True,
                "macro_csv": bool(macro),
                "news_calendar": str(NEWS),
                "market_levels": str(LEVELS),
                "openai_research": "config present; historical API replay not performed",
            },
            "window": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "cadence_minutes": args.cadence,
            "cadence_note": "Lower cadence (e.g. 15 or 5) walks more snapshot steps and may surface more unique candidates; default 60 balances runtime vs coverage.",
        },
        "discovery": {
            "total_unique_candidates": len(signals),
            "by_family": _count_by(signals, "family"),
            "by_tf": _count_by(signals, "tf"),
            "by_ifvg_grade": _count_by(ifvg_signals, "ifvg_grade"),
            "by_decision_status_at_scan": _count_by(signals, "decision_status"),
            "by_htf_bias": _count_by(signals, "htf_bias"),
            "by_alignment": _count_by(signals, "alignment"),
            "by_oscillation": _count_by(signals, "oscillation"),
        },
        "simulation": simulation_block,
    }

    logs = REPO / "logs"
    logs.mkdir(exist_ok=True)
    csv_path = logs / "full_entry_scan_signals.csv"
    json_path = logs / "full_entry_scan_report.json"

    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(SIGNAL_FIELDS), extrasaction="ignore")
        w.writeheader()
        w.writerows(signals)

    json_path.write_text(json.dumps(report, indent=2))

    print("\n" + "=" * 72)
    print("FULL ENTRY SCAN (unbiased discovery)")
    print("=" * 72)
    print(f"Signals CSV: {csv_path} ({len(signals)} rows)")
    print(f"Report JSON: {json_path}")
    print(f"By family: {report['discovery']['by_family']}")
    if simulation_block:
        print(f"Simulation (optional): {simulation_block['trades']} trades, net ${simulation_block['net_pnl']:+,.2f}")
    print("No manipulation gates applied to discovery (see methodology.no_filters_applied).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
