#!/usr/bin/env python3
"""Evaluate full_entry_scan_signals.csv — honest per-signal PnL slices.

Reads discovery CSV (all rows, no filter manipulation), simulates each signal
independently at $30 fixed risk using engine entry/stop/target, then ranks
slices by net PnL with a minimum trade count.

Outputs: logs/entry_scan_evaluation.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics as stats
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402

# Reuse simulation constants from full_entry_scan.py
CONTRACT = 100.0
MIN_LOT = 0.01
SPREAD_RT = 0.70
DEFAULT_RISK_USD = 30.0
MIN_SLICE_TRADES = 5


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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


def _score_band(score) -> str:
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "?"
    if s < 65:
        return "<65"
    if s < 80:
        return "65-79"
    if s < 90:
        return "80-89"
    return "90+"


def simulate_signal(s: dict, bars_by_tf: dict[int, list], *, risk_usd: float) -> dict | None:
    ts = _parse_ts(s["time"])
    tf = int(s["tf"])
    bars = bars_by_tf.get(tf) or []
    if not bars:
        return None
    idx = _find_bar_idx(bars, ts)
    entry = float(s["entry"])
    stop = float(s["stop"])
    target = float(s["target"])
    lots = _size_lots(entry, stop, risk_usd)
    if lots is None:
        return None
    horizon = {5: 576, 15: 384, 60: 168, 240: 96}.get(tf, 384)
    outcome, exit_px, _ = _simulate(
        bars, idx, side=s["side"], entry=entry, stop=stop, target=target, max_bars=horizon
    )
    if outcome == "open":
        return None
    spread = SPREAD_RT * (lots / MIN_LOT)
    if outcome == "win":
        move = abs(exit_px - entry)
        pnl = move * CONTRACT * lots - spread
    else:
        pnl = -abs(entry - stop) * CONTRACT * lots - spread
    return {
        **s,
        "score_band": _score_band(s.get("score")),
        "lots": lots,
        "outcome": outcome,
        "pnl_usd": round(pnl, 2),
    }


def _summarize(trades: list[dict]) -> dict:
    pnls = [float(t["pnl_usd"]) for t in trades]
    w = sum(1 for p in pnls if p > 0)
    n = len(pnls)
    return {
        "trades": n,
        "wins": w,
        "losses": n - w,
        "win_rate": round(w / n, 4) if n else 0,
        "net_pnl": round(sum(pnls), 2),
        "avg_pnl": round(stats.mean(pnls), 2) if pnls else 0,
    }


def _bucket(trades: list[dict], key_fn) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[str(key_fn(t))].append(t)
    return {k: _summarize(v) for k, v in sorted(groups.items())}


def _slice_key(trade: dict, dims: tuple[str, ...]) -> str:
    parts = []
    for d in dims:
        if d == "family" and trade.get("family") != "inversion_fair_value_gap":
            if "ifvg_grade" in dims or "ifvg" in dims:
                continue
        val = trade.get(d)
        if d == "ifvg_grade" and not val:
            val = "n/a"
        parts.append(f"{d}={val or '?'}")
    return "|".join(parts)


def _rank_slices(
    trades: list[dict],
    dim_sets: list[tuple[str, ...]],
    *,
    min_trades: int = MIN_SLICE_TRADES,
) -> tuple[list[dict], list[dict]]:
    best: list[dict] = []
    worst: list[dict] = []
    for dims in dim_sets:
        groups: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            if "ifvg_grade" in dims and t.get("family") != "inversion_fair_value_gap":
                continue
            groups[_slice_key(t, dims)].append(t)
        for label, group in groups.items():
            if len(group) < min_trades:
                continue
            summary = _summarize(group)
            row = {"slice": label, "dimensions": list(dims), **summary}
            if summary["net_pnl"] > 0:
                best.append(row)
            elif summary["net_pnl"] < 0:
                worst.append(row)
    best.sort(key=lambda r: (r["net_pnl"], r["win_rate"]), reverse=True)
    worst.sort(key=lambda r: (r["net_pnl"], r["win_rate"]))
    return best, worst


def _cross_tab(trades: list[dict], key_a: str, key_b: str, *, min_trades: int = MIN_SLICE_TRADES) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[f"{key_a}={t.get(key_a)}|{key_b}={t.get(key_b)}"].append(t)
    out = {}
    for k, v in groups.items():
        if len(v) < min_trades:
            continue
        s = _summarize(v)
        if s["net_pnl"] > 0:
            out[k] = s
    return dict(sorted(out.items(), key=lambda kv: kv[1]["net_pnl"], reverse=True))


def _data_layers_doc() -> dict:
    """Document included vs excluded layers (matches full_entry_scan + engine)."""
    return {
        "included": {
            "multi_tf_bars": "5m, 15m, 60m, 240m from scan data-dir",
            "bundle_htf_bias": "derived in analyze_timeframe_bundle → snap.higher_timeframe_bias",
            "bundle_alignment": "analysis.alignment_label",
            "bundle_oscillation": "oscillation_label on snapshot (recorded in CSV)",
            "macro_csv": "load_macro_frame(data/macro): IFVG macro_confirmation checklist + timed_horizon_macro_regime family",
            "news_calendar": "data/macro/news_calendar.csv — IFVG news warnings",
            "market_levels": "config/market_levels.json — static manual S/R & round/options reference levels (not live CME feed)",
            "openai_research": "config + cache only at scan time; force_external_research=False — no historical API replay; cache hit if key matches",
            "all_entry_families": "FULL_FAMILIES in full_entry_scan.py (9 families)",
            "ifvg_grading": "A/B/C from checklist score; verdict=ignore skipped in engine",
        },
        "excluded_or_not_live": {
            "options_flow_feed": "No dedicated options flow / gamma / OI time series in scan path",
            "cme_data_feed": "No CME API or futures OI pipeline — only manual market_levels labels and OpenAI prompt text when API runs",
            "openai_web_historical": "Backtest does not replay OpenAI web search per bar; neutral/cache unless live cache key matches",
            "macro_filter_hard_block": "MacroDecisionFilter (live_monitor) not applied to discovery",
            "bundle_decision_as_filter": "accept/reject/hold recorded as metadata only",
            "quality_gates_on_discovery": "No min RR, grade filter, or score threshold on CSV rows",
        },
        "note": "Non-IFVG families use HTF bias for scoring only; macro_frame only for IFVG checklist and timed_horizon_macro_regime.",
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Evaluate full entry scan signals by slice")
    p.add_argument("--signals", default=str(REPO / "logs" / "full_entry_scan_signals.csv"))
    p.add_argument("--report", default=str(REPO / "logs" / "full_entry_scan_report.json"))
    p.add_argument("--data-dir", default=str(REPO / "data" / "agent_live_xauusd"))
    p.add_argument("--end", default="2026-05-29")
    p.add_argument("--risk-usd", type=float, default=DEFAULT_RISK_USD)
    p.add_argument("--min-trades", type=int, default=MIN_SLICE_TRADES)
    p.add_argument("--out", default=str(REPO / "logs" / "entry_scan_evaluation.json"))
    args = p.parse_args()

    signals_path = Path(args.signals)
    if not signals_path.exists():
        print(f"Missing {signals_path}", file=sys.stderr)
        return 1

    with signals_path.open(newline="") as f:
        signals = list(csv.DictReader(f))

    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=23, minutes=59)
    bars_by_tf: dict[int, list] = {}
    for tf, fname in ((5, "5m"), (15, "15m"), (60, "60m"), (240, "240m")):
        path = Path(args.data_dir) / f"xauusd_{fname}.csv"
        if path.exists():
            bars_by_tf[tf] = [b for b in load_bars_from_csv(path) if b.timestamp <= end_dt]

    trades: list[dict] = []
    skipped_open = 0
    for s in signals:
        row = simulate_signal(s, bars_by_tf, risk_usd=args.risk_usd)
        if row is None:
            skipped_open += 1
        else:
            trades.append(row)

    dim_sets: list[tuple[str, ...]] = [
        ("tf",),
        ("family",),
        ("ifvg_grade",),
        ("htf_bias",),
        ("alignment",),
        ("decision_status",),
        ("score_band",),
        ("oscillation",),
        ("tf", "family"),
        ("tf", "ifvg_grade"),
        ("family", "ifvg_grade"),
        ("tf", "family", "htf_bias"),
        ("tf", "family", "decision_status"),
        ("tf", "ifvg_grade", "htf_bias"),
        ("tf", "ifvg_grade", "alignment"),
        ("tf", "ifvg_grade", "decision_status"),
        ("family", "htf_bias", "alignment", "decision_status"),
        ("tf", "family", "ifvg_grade", "htf_bias", "decision_status"),
        ("tf", "family", "ifvg_grade", "htf_bias", "alignment", "decision_status", "score_band"),
    ]

    best, worst = _rank_slices(trades, dim_sets, min_trades=args.min_trades)

    ifvg_trades = [t for t in trades if t.get("family") == "inversion_fair_value_gap"]
    family_tf_positive = _cross_tab(trades, "family", "tf", min_trades=args.min_trades)
    ifvg_grade_tf_positive = _cross_tab(ifvg_trades, "ifvg_grade", "tf", min_trades=args.min_trades)

    report_ref = {}
    rp = Path(args.report)
    if rp.exists():
        report_ref = json.loads(rp.read_text()).get("simulation") or {}

    out = {
        "methodology": {
            "source_csv_rows": len(signals),
            "simulation": "per_signal_independent (not one-position-at-a-time)",
            "risk_usd": args.risk_usd,
            "spread_rt_per_0_01_lot": SPREAD_RT,
            "uses_engine_entry_stop_target": True,
            "min_trades_per_slice": args.min_trades,
            "skipped_no_outcome_within_horizon": skipped_open,
            "simulated_trades": len(trades),
            "report_sequential_sim_note": (
                "full_entry_scan_report.json used one_position_at_a_time; "
                f"that produced {report_ref.get('trades', '?')} trades vs {len(trades)} here"
            ),
        },
        "data_layers": _data_layers_doc(),
        "overall": _summarize(trades) if trades else {},
        "by_dimension": {
            "tf": _bucket(trades, lambda t: t["tf"]),
            "family": _bucket(trades, lambda t: t["family"]),
            "ifvg_grade": _bucket(ifvg_trades, lambda t: t.get("ifvg_grade") or "?"),
            "htf_bias": _bucket(trades, lambda t: t["htf_bias"]),
            "alignment": _bucket(trades, lambda t: t["alignment"]),
            "decision_status": _bucket(trades, lambda t: t["decision_status"]),
            "score_band": _bucket(trades, lambda t: t["score_band"]),
            "oscillation": _bucket(trades, lambda t: t["oscillation"]),
        },
        "top_5_best_slices": best[:5],
        "top_5_worst_slices": worst[:5],
        "cross_tabs": {
            "positive_net_family_x_tf": dict(list(family_tf_positive.items())[:15]),
            "positive_net_ifvg_grade_x_tf": dict(list(ifvg_grade_tf_positive.items())[:15]),
        },
        "actionable_rules_draft": _actionable_rules(best[:10], worst[:10]),
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))

    print(json.dumps({
        "out": str(out_path),
        "signals": len(signals),
        "simulated": len(trades),
        "overall_net": out["overall"].get("net_pnl"),
        "top_best": [r["slice"] for r in out["top_5_best_slices"]],
        "top_worst": [r["slice"] for r in out["top_5_worst_slices"]],
    }, indent=2))
    return 0


def _actionable_rules(best: list[dict], worst: list[dict]) -> list[str]:
    rules: list[str] = []
    if best:
        b = best[0]
        rules.append(
            f"Prioritize slice with highest net in sample: {b['slice']} "
            f"({b['trades']} trades, WR {b['win_rate']:.0%}, net ${b['net_pnl']:+.0f})."
        )
    for w in worst[:3]:
        rules.append(f"Avoid or paper-only: {w['slice']} (net ${w['net_pnl']:+.0f}, {w['trades']} trades).")
    rules.append(
        "Treat bundle decision_status=reject as weak live filter — report sequential sim showed rejects outperformed accepts; verify with per-signal slices."
    )
    rules.append(
        "IFVG: require grade A or B and htf_bias alignment before live size; ignore verdict=ignore (not in CSV)."
    )
    rules.append(
        "Do not assume OpenAI/CME/options flow in backtest — refresh market_levels.json live and enable research only forward."
    )
    return rules


if __name__ == "__main__":
    raise SystemExit(main())
