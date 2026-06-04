#!/usr/bin/env python3
"""Evaluate full_stack_scan_signals.csv — combination finder + sequential sim.

Grid/slice search across family × tf × ifvg_grade × htf_bias × alignment ×
macro_regime × openai_support × news_clear × score_band. Ranks combinations
(min 5 trades), reports best/worst 10, sequential one-position sim for top 5
vs baseline.

Outputs:
  logs/full_stack_evaluation.json
  logs/full_stack_best_combos.json
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
from itertools import product
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402

CONTRACT = 100.0
MIN_LOT = 0.01
SPREAD_RT = 0.70
DEFAULT_RISK_USD = 30.0
MIN_SLICE_TRADES = 5

COMBO_DIMS = (
    "family",
    "tf",
    "ifvg_grade",
    "htf_bias",
    "alignment",
    "macro_regime",
    "openai_support",
    "news_clear",
    "score_band",
)


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
        s = int(float(score))
    except (TypeError, ValueError):
        return "?"
    if s < 65:
        return "<65"
    if s < 80:
        return "65-79"
    if s < 90:
        return "80-89"
    return "90+"


def _openai_support(row: dict) -> str:
    status = str(row.get("openai_status") or "")
    if status == "research_unavailable_at_signal":
        return "unavailable"
    if status in {"", "unknown"}:
        return "unavailable"
    if str(row.get("openai_supports_trade")).lower() in {"true", "1"}:
        return "supports"
    conf = int(float(row.get("openai_confidence") or 0))
    if conf >= 55:
        return "supports"
    bias = str(row.get("openai_bias") or "unknown")
    if bias in {"bullish_gold", "bearish_gold"}:
        side = row.get("side", "")
        if (bias == "bullish_gold" and side == "long") or (bias == "bearish_gold" and side == "short"):
            return "supports"
    return "neutral"


def _news_clear(row: dict) -> str:
    v = row.get("news_blackout")
    if str(v).lower() in {"true", "1", "yes"}:
        return "blackout"
    return "clear"


def _enrich_row(row: dict) -> dict:
    out = dict(row)
    out["score_band"] = _score_band(row.get("score"))
    out["openai_support"] = _openai_support(row)
    out["news_clear"] = _news_clear(row)
    if row.get("family") != "inversion_fair_value_gap":
        out["ifvg_grade"] = "n/a"
    elif not out.get("ifvg_grade"):
        out["ifvg_grade"] = "?"
    out["macro_regime"] = row.get("macro_regime") or "unavailable"
    return out


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
    outcome, exit_px, exit_idx = _simulate(
        bars, idx, side=s["side"], entry=entry, stop=stop, target=target, max_bars=horizon
    )
    if outcome == "open":
        return None
    spread = SPREAD_RT * (lots / MIN_LOT)
    if outcome == "win":
        pnl = abs(exit_px - entry) * CONTRACT * lots - spread
    else:
        pnl = -abs(entry - stop) * CONTRACT * lots - spread
    return {**_enrich_row(s), "lots": lots, "outcome": outcome, "pnl_usd": round(pnl, 2)}


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


def _combo_key(trade: dict, dims: tuple[str, ...]) -> str:
    parts = []
    for d in dims:
        parts.append(f"{d}={trade.get(d) or '?'}")
    return "|".join(parts)


def _parse_combo_key(key: str) -> dict[str, str]:
    rules: dict[str, str] = {}
    for part in key.split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            rules[k] = v
    return rules


def _trade_matches(trade: dict, rules: dict[str, str]) -> bool:
    for dim, want in rules.items():
        if trade.get(dim) != want:
            return False
    return True


def _rules_fingerprint(rules: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(rules.items()))


def _rank_combinations(trades: list[dict], *, min_trades: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Systematic slices: singles, pairs, and full grid on core dims."""
    dim_sets: list[tuple[str, ...]] = []
    for n in (1, 2, 3):
        for combo in product(COMBO_DIMS, repeat=n):
            if "ifvg_grade" in combo and "family" not in combo:
                continue
            dim_sets.append(combo)

    dim_sets.extend([
        ("family", "tf", "htf_bias"),
        ("family", "tf", "ifvg_grade", "htf_bias"),
        ("family", "tf", "ifvg_grade", "htf_bias", "alignment"),
        ("family", "tf", "ifvg_grade", "htf_bias", "alignment", "macro_regime"),
        ("family", "tf", "ifvg_grade", "htf_bias", "alignment", "macro_regime", "openai_support"),
        ("family", "tf", "ifvg_grade", "htf_bias", "alignment", "macro_regime", "openai_support", "news_clear"),
        ("family", "tf", "htf_bias", "alignment", "macro_regime", "news_clear", "score_band"),
        COMBO_DIMS,
    ])

    seen_fp: set[tuple[tuple[str, str], ...]] = set()
    ranked: list[dict] = []
    for dims in dim_sets:
        groups: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            if "ifvg_grade" in dims and t.get("family") != "inversion_fair_value_gap":
                continue
            groups[_combo_key(t, dims)].append(t)
        for key, group in groups.items():
            if len(group) < min_trades:
                continue
            rules = _parse_combo_key(key)
            fp = _rules_fingerprint(rules)
            if fp in seen_fp:
                continue
            seen_fp.add(fp)
            summary = _summarize(group)
            ranked.append({
                "combo_key": key,
                "rules": rules,
                "dimensions": list(dims),
                **summary,
            })

    ranked.sort(key=lambda r: (r["net_pnl"], r["win_rate"]), reverse=True)
    best = [r for r in ranked if r["net_pnl"] > 0][:10]
    worst = sorted([r for r in ranked if r["net_pnl"] < 0], key=lambda r: (r["net_pnl"], r["win_rate"]))[:10]
    actionable = [
        r for r in ranked
        if r["net_pnl"] > 0 and "family" in r["rules"] and "tf" in r["rules"]
    ][:10]
    return best, worst, actionable


def _sequential_sim(trades: list[dict], bars_by_tf: dict[int, list], *, risk_usd: float) -> dict:
    rows = sorted(trades, key=lambda r: r["time"])
    executed: list[dict] = []
    busy_until: datetime | None = None

    for s in rows:
        ts = _parse_ts(s["time"])
        if busy_until and ts <= busy_until:
            continue
        tf = int(s["tf"])
        bars = bars_by_tf.get(tf) or []
        idx = _find_bar_idx(bars, ts)
        entry = float(s["entry"])
        stop = float(s["stop"])
        target = float(s["target"])
        lots = _size_lots(entry, stop, risk_usd)
        if lots is None:
            continue
        horizon = {5: 576, 15: 384, 60: 168, 240: 96}.get(tf, 384)
        outcome, exit_px, exit_idx = _simulate(
            bars, idx, side=s["side"], entry=entry, stop=stop, target=target, max_bars=horizon
        )
        if outcome == "open":
            continue
        spread = SPREAD_RT * (lots / MIN_LOT)
        if outcome == "win":
            pnl = abs(exit_px - entry) * CONTRACT * lots - spread
        else:
            pnl = -abs(entry - stop) * CONTRACT * lots - spread
        busy_until = bars[exit_idx].timestamp
        executed.append({**s, "outcome": outcome, "pnl_usd": round(pnl, 2)})

    return _summarize(executed) if executed else {"trades": 0, "net_pnl": 0, "win_rate": 0}


def _trading_profile(
    best: list[dict],
    worst: list[dict],
    actionable: list[dict],
) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "recommended_filters": [],
        "avoid_filters": [],
        "scout_start_hints": [],
        "live_notes": [],
    }
    primary = actionable[0] if actionable else (best[0] if best else None)
    if primary:
        profile["recommended_filters"] = [primary["rules"]]
        profile["scout_start_hints"].append(
            f"Prefer combo: {primary['combo_key']} ({primary['trades']} trades, WR {primary['win_rate']:.0%}, net ${primary['net_pnl']:+.0f})"
        )
    if best and best[0] != primary:
        b = best[0]
        profile["scout_start_hints"].append(
            f"Broad sentiment slice (all families): {b['combo_key']} (net ${b['net_pnl']:+.0f})"
        )
    for w in worst[:3]:
        profile["avoid_filters"].append(w["rules"])
    profile["live_notes"] = [
        "Use bundle decision_status as metadata — verify accept vs reject in your window.",
        "IFVG: require workflow_ready=true and grade A/B when filtering live.",
        "Refresh config/market_levels.json; CME/options are proxy-only in backtest.",
        "OpenAI: forward-only via --with-openai-live; historical rows use cache hour-bucket match.",
        "Respect news_blackout=true rows in live (cli news_blackout_min).",
    ]
    return profile


def main() -> int:
    p = argparse.ArgumentParser(description="Evaluate full-stack scan combinations")
    p.add_argument("--signals", default=str(REPO / "logs" / "full_stack_scan_signals.csv"))
    p.add_argument("--data-dir", default=str(REPO / "data" / "agent_live_xauusd"))
    p.add_argument("--end", default="2026-05-29")
    p.add_argument("--risk-usd", type=float, default=DEFAULT_RISK_USD)
    p.add_argument("--min-trades", type=int, default=MIN_SLICE_TRADES)
    p.add_argument("--out-eval", default=str(REPO / "logs" / "full_stack_evaluation.json"))
    p.add_argument("--out-combos", default=str(REPO / "logs" / "full_stack_best_combos.json"))
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
    skipped = 0
    for s in signals:
        row = simulate_signal(s, bars_by_tf, risk_usd=args.risk_usd)
        if row is None:
            skipped += 1
        else:
            trades.append(row)

    best, worst, actionable = _rank_combinations(trades, min_trades=args.min_trades)

    baseline_seq = _sequential_sim(trades, bars_by_tf, risk_usd=args.risk_usd)
    top5_seq: list[dict] = []
    seq_source = actionable[:5] if actionable else best[:5]
    for combo in seq_source:
        filtered = [t for t in trades if _trade_matches(t, combo["rules"])]
        seq = _sequential_sim(filtered, bars_by_tf, risk_usd=args.risk_usd)
        top5_seq.append({"combo_key": combo["combo_key"], "rules": combo["rules"], "sequential": seq})

    profile = _trading_profile(best, worst, actionable)

    evaluation = {
        "methodology": {
            "source_rows": len(signals),
            "simulated_trades": len(trades),
            "skipped_open": skipped,
            "simulation": "per_signal_independent for ranking; sequential for top combos",
            "risk_usd": args.risk_usd,
            "min_trades_per_combo": args.min_trades,
            "combo_dimensions": list(COMBO_DIMS),
        },
        "overall": _summarize(trades) if trades else {},
        "best_10_combinations": best,
        "best_10_actionable_family_tf": actionable,
        "worst_10_combinations": worst,
        "sequential_simulation": {
            "baseline_all_signals": baseline_seq,
            "top_5_combos": top5_seq,
        },
        "trading_profile": profile,
        "coverage_gaps": {
            "cme_live_feed": False,
            "openai_historical": "cache hour-bucket only unless cache_hit",
            "workflow_m1": "M1 bars not in agent_live_xauusd — LTF step may wait",
        },
    }

    combos_out = {
        "window_note": "Apr 29 – May 29 2026 agent_live_xauusd",
        "best_10": best,
        "best_10_actionable_family_tf": actionable,
        "worst_10": worst,
        "actionable_profile": profile,
        "sequential_vs_baseline": {
            "baseline": baseline_seq,
            "top_5": top5_seq,
        },
    }

    Path(args.out_eval).parent.mkdir(exist_ok=True)
    Path(args.out_eval).write_text(json.dumps(evaluation, indent=2))
    Path(args.out_combos).write_text(json.dumps(combos_out, indent=2))

    print(json.dumps({
        "eval": args.out_eval,
        "combos": args.out_combos,
        "signals": len(signals),
        "simulated": len(trades),
        "best_combo": best[0]["combo_key"] if best else None,
        "best_net": best[0]["net_pnl"] if best else None,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
