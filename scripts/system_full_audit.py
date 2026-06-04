#!/usr/bin/env python3
"""30-day FULL SYSTEM audit — not IFVG-only.

Replays ``build_bundle_snapshot`` (research/state.py) bar-by-bar with:
  - All entry families wired in state.py (IFVG + breakouts + sweeps + macro)
  - Multi-TF bundle analysis (trend, alignment, HTF bias, oscillation regime)
  - Macro CSV, news calendar, market_levels.json
  - IFVG A/B/C/D grades extracted from candidate details
  - Bundle decision plan (accept / reject / hold)

OpenAI web research is OFF (no historical API replay). Everything else local.

Outputs: logs/system_full_audit_report.json, logs/system_full_audit_trades.csv
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

from gold_trader.calendar import NewsCalendar  # noqa: E402
from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.research import build_bundle_snapshot  # noqa: E402

CONTRACT = 100.0
MIN_LOT = 0.01
SPREAD_RT = 0.70
RISK_USD = 30.0

LEVELS = REPO / "config" / "market_levels.json"
NEWS = REPO / "data" / "macro" / "news_calendar.csv"
OPENAI_CFG = REPO / "config" / "openai_research.json"
SHADOW = REPO / "logs" / "ifvg_shadow_journal.csv"

# Every family wired in research/state.py _entry_candidates
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


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _slice_bars(bars, end: datetime):
    return [b for b in bars if b.timestamp <= end]


def _size_lots(entry: float, stop: float, risk_usd: float) -> float | None:
    sl = abs(entry - stop)
    if sl <= 0:
        return None
    lots = risk_usd / (sl * CONTRACT)
    return max(MIN_LOT, min(0.05, math.floor(lots / MIN_LOT) * MIN_LOT))


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


def _find_bar_idx(bars, ts: datetime) -> int:
    for i, b in enumerate(bars):
        if b.timestamp == ts:
            return i
    # nearest prior
    idx = 0
    for i, b in enumerate(bars):
        if b.timestamp <= ts:
            idx = i
        else:
            break
    return idx


def _ifvg_grade(candidate) -> str:
    details = candidate.details or {}
    grading = details.get("grading") or {}
    return str(grading.get("letter") or details.get("grade") or "?")


def _ifvg_tech_score(candidate) -> int:
    details = candidate.details or {}
    return int(details.get("score") or candidate.score or 0)


def collect_signals(
    all_bars: dict[int, list],
    *,
    start: datetime,
    end: datetime,
    macro,
    cadence_minutes: int = 60,
) -> list[dict]:
    """Walk clock at cadence (default M60); rebuild bundle snapshot on truncated history."""
    cadence = cadence_minutes
    anchor_bars = all_bars.get(cadence) or all_bars.get(15) or []
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
            if len(_slice_bars(bars, anchor.timestamp)) >= 80
        }
        if len(datasets) < 2:
            continue

        snap = build_bundle_snapshot(
            datasets,
            families=FULL_FAMILIES,
            max_candidates=12,
            macro_frame=macro,
            market_levels_path=str(LEVELS),
            news_calendar_path=str(NEWS),
            shadow_journal_path=str(SHADOW),
            openai_research_config_path=str(OPENAI_CFG),
            openai_research_cache_path=str(REPO / "data/cache/openai_market_research.json"),
        )

        top = snap.entry_candidates[0] if snap.entry_candidates else None
        decision = snap.decision

        for cand in snap.entry_candidates:
            tf_bars = datasets.get(cand.timeframe_minutes) or []
            if not tf_bars:
                continue
            bar_idx = len(tf_bars) - 1
            key = (
                cand.family,
                cand.timeframe_minutes,
                cand.side.value,
                tf_bars[bar_idx].timestamp.isoformat(),
                round(cand.reference_price, 2),
                round(cand.stop, 2),
            )
            if key in seen:
                continue
            seen.add(key)

            rr = 0.0
            sl_dist = abs(cand.reference_price - cand.stop)
            if sl_dist > 0:
                rr = abs(cand.target - cand.reference_price) / sl_dist

            signals.append({
                "time": tf_bars[bar_idx].timestamp.isoformat(),
                "family": cand.family,
                "tf": cand.timeframe_minutes,
                "side": cand.side.value,
                "score": cand.score,
                "regime_fit": cand.regime_fit,
                "reason": cand.reason,
                "conflict": cand.conflict,
                "entry": cand.reference_price,
                "stop": cand.stop,
                "target": cand.target,
                "rr": round(rr, 3),
                "sl_dist": round(sl_dist, 2),
                "bar_idx": bar_idx,
                "ifvg_grade": _ifvg_grade(cand) if cand.family == "inversion_fair_value_gap" else None,
                "ifvg_tech_score": _ifvg_tech_score(cand) if cand.family == "inversion_fair_value_gap" else None,
                "ifvg_verdict": (cand.details or {}).get("verdict") if cand.family == "inversion_fair_value_gap" else None,
                "bundle_alignment": snap.alignment_label,
                "htf_bias": snap.higher_timeframe_bias,
                "oscillation": snap.oscillation_label,
                "decision_status": decision.status,
                "decision_family": decision.family,
                "decision_is_top": top is not None and cand.family == top.family and cand.timeframe_minutes == top.timeframe_minutes and cand.side is top.side,
                "warnings": list(snap.warnings),
            })

    return signals


def execute(signals: list[dict], bars_by_tf: dict[int, list], *, one_position: bool, risk_usd: float) -> list[dict]:
    rows = sorted(signals, key=lambda r: r["time"])
    busy_until: datetime | None = None
    executed: list[dict] = []

    for s in rows:
        ts = _parse_ts(s["time"])
        if one_position and busy_until and ts <= busy_until:
            continue
        tf = int(s["tf"])
        bars = bars_by_tf.get(tf) or []
        idx = _find_bar_idx(bars, ts)
        lots = _size_lots(s["entry"], s["stop"], risk_usd)
        if lots is None:
            continue
        horizon = {5: 576, 15: 384, 60: 168, 240: 96}.get(tf, 384)
        outcome, exit_px, exit_idx = _simulate(
            bars, idx, side=s["side"], entry=s["entry"], stop=s["stop"], target=s["target"], max_bars=horizon,
        )
        if outcome == "open":
            continue
        spread = SPREAD_RT * (lots / MIN_LOT)
        if outcome == "win":
            move = abs(exit_px - s["entry"])
            pnl = move * CONTRACT * lots - spread
        else:
            pnl = -abs(s["entry"] - s["stop"]) * CONTRACT * lots - spread

        if one_position:
            busy_until = bars[exit_idx].timestamp

        executed.append({**s, "lots": lots, "outcome": outcome, "pnl_usd": round(pnl, 2), "exit_time": bars[exit_idx].timestamp.isoformat()})

    return executed


def _summarize(trades: list[dict], key_fn) -> dict[str, dict]:
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


def _elimination_advice(by_grade: dict[str, dict], by_family: dict[str, dict]) -> list[str]:
    tips: list[str] = []
    for grade, sm in sorted(by_grade.items()):
        if sm["trades"] < 5:
            tips.append(f"IFVG grade {grade}: only {sm['trades']} trades — insufficient sample, keep watching.")
        elif sm["net_pnl"] < -50:
            tips.append(f"ELIMINATE or block IFVG grade {grade}: {sm['trades']} trades, net ${sm['net_pnl']:+.0f}, WR {sm['win_rate']*100:.0f}%.")
        elif sm["net_pnl"] > 0:
            tips.append(f"KEEP IFVG grade {grade}: net ${sm['net_pnl']:+.0f} over {sm['trades']} trades.")
    for fam, sm in sorted(by_family.items(), key=lambda x: x[1]["net_pnl"]):
        if sm["trades"] < 3:
            continue
        if sm["net_pnl"] < -100:
            tips.append(f"DISABLE family {fam}: net ${sm['net_pnl']:+.0f} ({sm['trades']} trades).")
        elif sm["net_pnl"] > 50:
            tips.append(f"PROMOTE family {fam}: net ${sm['net_pnl']:+.0f} ({sm['trades']} trades).")
    return tips


def main() -> int:
    p = argparse.ArgumentParser(description="Full system 30-day audit (all families + sentiment)")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--end", default="2026-05-29")
    p.add_argument("--data-dir", default=str(REPO / "data" / "agent_live_xauusd"))
    p.add_argument("--cadence", type=int, default=60, help="Snapshot every N minutes (60=M60 bar)")
    p.add_argument("--risk-usd", type=float, default=RISK_USD)
    args = p.parse_args()

    risk_usd = args.risk_usd

    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=23, minutes=59)
    start_dt = end_dt - timedelta(days=args.days)
    data_dir = Path(args.data_dir)

    macro = load_macro_frame(REPO / "data" / "macro")
    if macro is not None and not macro.names():
        macro = None

    all_bars: dict[int, list] = {}
    for tf, fname in ((5, "5m"), (15, "15m"), (60, "60m"), (240, "240m")):
        path = data_dir / f"xauusd_{fname}.csv"
        if not path.exists():
            continue
        full = load_bars_from_csv(path)
        all_bars[tf] = [b for b in full if b.timestamp <= end_dt]

    print(f"Collecting full-system signals {start_dt.date()} → {end_dt.date()}...", flush=True)
    signals = collect_signals(all_bars, start=start_dt, end=end_dt, macro=macro, cadence_minutes=args.cadence)
    print(f"  Unique entry candidates: {len(signals)}", flush=True)

    # Execution scenarios
    ex_all = execute(signals, all_bars, one_position=True, risk_usd=risk_usd)
    ex_accept = execute([s for s in signals if s["decision_status"] == "accept" and s["decision_is_top"]], all_bars, one_position=True, risk_usd=risk_usd)
    ifvg = [s for s in signals if s["family"] == "inversion_fair_value_gap"]
    ex_ifvg = execute(ifvg, all_bars, one_position=False, risk_usd=risk_usd)

    by_family = _summarize(ex_all, lambda t: t["family"])
    by_ifvg_grade = _summarize(
        [t for t in ex_all if t["family"] == "inversion_fair_value_gap" and t.get("ifvg_grade")],
        lambda t: t["ifvg_grade"],
    )
    by_decision = _summarize(ex_all, lambda t: t["decision_status"])
    by_htf = _summarize(ex_all, lambda t: t["htf_bias"])
    by_alignment = _summarize(ex_all, lambda t: t["bundle_alignment"])
    by_score_band = _summarize(ex_all, lambda t: "80+" if t["score"] >= 80 else ("70-79" if t["score"] >= 70 else ("65-69" if t["score"] >= 65 else "<65")))

    report = {
        "methodology": {
            "engine": "gold_trader.research.state.build_bundle_snapshot",
            "families": list(FULL_FAMILIES),
            "data_layers": {
                "multi_tf_bundle_analysis": "trend_state, RSI, MACD, structure, alignment_label",
                "htf_bias": "from bundle analysis (bullish/bearish/neutral)",
                "oscillation_regime": "oscillation_label from bundle",
                "macro_csv": "dxy, us10y, real10y" if macro else "unavailable",
                "news_calendar": str(NEWS),
                "market_levels": str(LEVELS),
                "ifvg_checklist": "inside IFVG family only",
                "openai_research": "DISABLED — no historical replay",
            },
            "window": f"{start_dt.date()} → {end_dt.date()}",
            "execution": f"fixed ${risk_usd} risk/trade, honest SL/target, spread ${SPREAD_RT}/0.01 lot",
            "what_prior_ifvg_audits_did_NOT_include": [
                "Other strategy families (breakouts, sweeps, momentum, macro regime)",
                "Bundle decision plan accept/reject/hold gates",
                "Multi-TF alignment / oscillation sentiment",
                "Per-family regime scoring",
                "Grade elimination analysis across all families",
            ],
            "what_live_start_still_differs": [
                "./start runs IFVG scout only — not full bundle snapshot on every tick",
                "8-step IFVG workflow UI gates not applied here",
                "OpenAI web sentiment not replayed",
                "Operator manual skip/approve not modeled",
            ],
        },
        "signal_counts": {
            "total_unique_candidates": len(signals),
            "by_family": dict(Counter(s["family"] for s in signals)),
            "by_ifvg_grade": dict(Counter(s["ifvg_grade"] for s in ifvg if s.get("ifvg_grade"))),
            "by_decision_status_at_scan": dict(Counter(s["decision_status"] for s in signals)),
            "by_htf_bias": dict(Counter(s["htf_bias"] for s in signals)),
            "by_alignment": dict(Counter(s["bundle_alignment"] for s in signals)),
        },
        "execution_all_families_one_position": {
            "trades": len(ex_all),
            "net_pnl": round(sum(t["pnl_usd"] for t in ex_all), 2),
            "by_family": by_family,
            "by_ifvg_grade": by_ifvg_grade,
            "by_decision_status": by_decision,
            "by_htf_bias": by_htf,
            "by_alignment": by_alignment,
            "by_score_band": by_score_band,
        },
        "execution_bundle_accept_only": {
            "trades": len(ex_accept),
            "net_pnl": round(sum(t["pnl_usd"] for t in ex_accept), 2),
        },
        "execution_ifvg_all_grades_parallel": {
            "trades": len(ex_ifvg),
            "net_pnl": round(sum(t["pnl_usd"] for t in ex_ifvg), 2),
            "by_grade": _summarize(ex_ifvg, lambda t: t.get("ifvg_grade") or "?"),
        },
        "grade_elimination_recommendations": _elimination_advice(by_ifvg_grade, by_family),
    }

    out_json = REPO / "logs" / "system_full_audit_report.json"
    out_json.parent.mkdir(exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2))

    csv_path = REPO / "logs" / "system_full_audit_trades.csv"
    if ex_all:
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(ex_all[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(ex_all)

    print("\n" + "=" * 72)
    print("FULL SYSTEM 30-DAY AUDIT (all families + bundle sentiment)")
    print("=" * 72)
    print(f"Signals: {len(signals)} unique candidates")
    print(f"Families: {report['signal_counts']['by_family']}")
    print(f"IFVG grades (signals): {report['signal_counts']['by_ifvg_grade']}")
    print(f"HTF bias mix: {report['signal_counts']['by_htf_bias']}")
    print(f"Alignment mix: {report['signal_counts']['by_alignment']}")
    print()
    print(f"EXECUTION (one position, all families, ${risk_usd:.0f} risk):")
    sm = report["execution_all_families_one_position"]
    print(f"  trades={sm['trades']} net=${sm['net_pnl']:+,.2f}")
    print("  By family:")
    for fam, d in sorted(sm["by_family"].items(), key=lambda x: x[1]["net_pnl"], reverse=True):
        print(f"    {fam:30s} n={d['trades']:3d} WR={d['win_rate']*100:5.1f}% net=${d['net_pnl']:+8.2f}")
    print("  IFVG by grade:")
    for g, d in sorted(sm.get("by_ifvg_grade", {}).items()):
        print(f"    grade {g}: n={d['trades']:3d} WR={d['win_rate']*100:5.1f}% net=${d['net_pnl']:+8.2f}")
    print()
    print("Bundle ACCEPT-only top candidate:", report["execution_bundle_accept_only"])
    print()
    print("GRADE / FAMILY RECOMMENDATIONS:")
    for line in report["grade_elimination_recommendations"]:
        print(f"  • {line}")
    print()
    print(f"Report: {out_json}")
    if ex_all:
        print(f"Trades: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
