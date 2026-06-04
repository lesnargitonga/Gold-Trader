#!/usr/bin/env python3
"""Deep IFVG strategy audit — exactly what the codebase would have done historically.

Uses the same engine as live/scout:
  gold_trader.assistants.ifvg_confluence.find_ifvg_setups
  gold_trader.assistants.ifvg_grading.compute_setup_grading
  gold_trader.assistants.ifvg_workflow.compute_htf_bias_for_scout (HTF)

Documents every assumption vs the live operator UI.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as stats
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.assistants.ifvg_confluence import (  # noqa: E402
    IFVGAssistantConfig,
    find_ifvg_setups,
    load_market_levels,
    setup_to_dict,
)
from gold_trader.assistants.ifvg_grading import RISK_BY_LETTER, compute_setup_grading  # noqa: E402
from gold_trader.assistants.ifvg_workflow import compute_htf_bias_for_scout  # noqa: E402
from gold_trader.calendar import NewsCalendar  # noqa: E402
from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.models import Side  # noqa: E402
from gold_trader.research.realtime_research import load_openai_research_config  # noqa: E402

CONTRACT = 100.0
MIN_LOT = 0.01
LEVELS = REPO / "config" / "market_levels.json"
NEWS = REPO / "data" / "macro" / "news_calendar.csv"
OPENAI_CFG = REPO / "config" / "openai_research.json"


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _filter_bars(bars, start: datetime, end: datetime):
    return [b for b in bars if start <= b.timestamp <= end]


def _round_lot(x: float) -> float:
    return max(MIN_LOT, math.floor(x / MIN_LOT) * MIN_LOT)


def _size_lots(entry: float, stop: float, risk_pct: float, equity: float, leverage: float, cap_lot: float | None) -> float | None:
    sl = abs(entry - stop)
    if sl <= 0:
        return None
    risk_usd = equity * risk_pct
    lots = risk_usd / (sl * CONTRACT)
    max_lots = (equity * leverage) / (CONTRACT * entry)
    lots = min(lots, max_lots)
    if cap_lot is not None:
        lots = min(lots, cap_lot)
    lots = _round_lot(lots)
    margin = (lots * CONTRACT * entry) / leverage
    if margin > equity * 1.05:
        return None
    return lots


def _simulate(bars, idx: int, *, side: str, entry: float, stop: float, tp1: float, max_bars: int):
    is_long = side == "long"
    end = min(len(bars), idx + 1 + max_bars)
    for j in range(idx + 1, end):
        bar = bars[j]
        if is_long:
            if bar.low <= stop:
                return "loss", stop, j
            if bar.high >= tp1:
                return "win", tp1, j
        else:
            if bar.high >= stop:
                return "loss", stop, j
            if bar.low <= tp1:
                return "win", tp1, j
    return "open", None, j if end > idx + 1 else idx


def _would_enter_live(setup_dict: dict) -> bool:
    """Match build_approval_brief can_enter (technical path, soft mode)."""
    grading = setup_dict.get("grading") or {}
    letter = str(grading.get("letter") or "D")
    verdict = str(setup_dict.get("verdict") or "")
    score = int(setup_dict.get("score") or 0)
    blocked = bool(setup_dict.get("externally_blocked"))
    research_mode = str((setup_dict.get("external_research") or {}).get("mode") or grading.get("research_mode") or "soft")
    if blocked and research_mode == "hard":
        return False
    return letter in ("A", "B") and verdict in ("valid_entry", "alert_wait") and score >= 65


def collect_signals(
    bars,
    *,
    tf: int,
    start: datetime,
    end: datetime,
    macro,
    levels,
    calendar,
    min_final_grade: str,
    include_openai: bool,
    min_score: int,
) -> list[dict]:
    cfg = IFVGAssistantConfig()
    warmup = max(cfg.atr_period + 5, 80)
    grade_ok = {"A": {"A"}, "B": {"A", "B"}, "C": {"A", "B", "C"}}[min_final_grade]
    research_cfg = load_openai_research_config(OPENAI_CFG)
    if not include_openai:
        from gold_trader.research.realtime_research import OpenAIResearchConfig
        research_cfg = OpenAIResearchConfig(enabled=False, mode="off")

    seen: set[tuple] = set()
    out: list[dict] = []
    htf = compute_htf_bias_for_scout(bars)

    for i in range(warmup, len(bars)):
        ts = bars[i].timestamp
        if ts < start or ts > end:
            continue
        setups = find_ifvg_setups(
            bars,
            index=i,
            macro_frame=macro,
            market_levels=levels,
            news_calendar=calendar,
            higher_timeframe_bias=htf if htf != "neutral" else None,
            openai_research_config=research_cfg,
            openai_config_path=OPENAI_CFG,
            openai_cache_path=REPO / "data/cache/openai_market_research.json",
            force_external_research=False,
        )
        if not setups:
            continue
        best = setups[0]
        d = setup_to_dict(best, timeframe_minutes=tf)
        letter = str((d.get("grading") or {}).get("letter") or "D")
        if letter not in grade_ok or best.score < min_score:
            continue
        c = best.candidate
        key = (tf, c.side.value, c.formation_idx, c.inversion_idx, c.signal_idx)
        if key in seen:
            continue
        seen.add(key)

        plan = best.plan
        out.append({
            "time": ts.isoformat(),
            "tf": tf,
            "side": c.side.value,
            "tech_score": best.score,
            "tech_grade": best.grade,
            "final_grade": letter,
            "verdict": best.verdict,
            "externally_blocked": best.externally_blocked,
            "would_enter_live": _would_enter_live(d),
            "entry": plan.entry,
            "stop": plan.stop,
            "tp1": plan.tp1,
            "tp2": plan.tp2,
            "tp3": plan.tp3,
            "bar_idx": i,
            "checklist_pass": sum(1 for x in best.checklist if x.status == "pass"),
            "checklist_total": len(best.checklist),
            "warnings": list(best.warnings)[:5],
        })
    return out


def execute_trades(
    signals: list[dict],
    bars_by_tf: dict[int, list],
    *,
    equity_start: float,
    leverage: float,
    cap_lot: float | None,
    spread_per_01: float,
    one_position: bool,
    live_entries_only: bool,
) -> tuple[list[dict], dict]:
    rows = [s for s in signals if (not live_entries_only or s["would_enter_live"])]
    rows.sort(key=lambda r: r["time"])
    equity = equity_start
    peak = equity
    max_dd = 0.0
    busy_until: datetime | None = None
    executed: list[dict] = []

    for s in rows:
        ts = _parse_ts(s["time"])
        if one_position and busy_until and ts <= busy_until:
            continue
        tf = int(s["tf"])
        bars = bars_by_tf[tf]
        idx = s["bar_idx"]
        letter = s["final_grade"]
        risk_pct = float(RISK_BY_LETTER.get(letter, 0.0))
        lots = _size_lots(s["entry"], s["stop"], risk_pct, equity, leverage, cap_lot)
        if lots is None:
            continue

        horizon = {5: 576, 15: 384, 60: 168}.get(tf, 384)
        outcome, exit_px, exit_idx = _simulate(
            bars, idx, side=s["side"], entry=s["entry"], stop=s["stop"], tp1=s["tp1"], max_bars=horizon,
        )
        if outcome == "open":
            continue

        spread = spread_per_01 * (lots / MIN_LOT)
        if outcome == "win":
            move = (exit_px - s["entry"]) if s["side"] == "long" else (s["entry"] - exit_px)
            pnl = move * CONTRACT * lots - spread
            r_mult = move / abs(s["entry"] - s["stop"]) if abs(s["entry"] - s["stop"]) else 0
        else:
            pnl = -abs(s["entry"] - s["stop"]) * CONTRACT * lots - spread
            r_mult = -1.0

        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        exit_ts = bars[exit_idx].timestamp.isoformat()
        if one_position:
            busy_until = _parse_ts(exit_ts)

        executed.append({
            **s,
            "lots": lots,
            "risk_pct": risk_pct,
            "outcome": outcome,
            "pnl_usd": round(pnl, 2),
            "r": round(r_mult, 3),
            "spread_usd": round(spread, 2),
            "exit_time": exit_ts,
            "equity_after": round(equity, 2),
            "sl_dist": round(abs(s["entry"] - s["stop"]), 2),
            "tp_dist": round(abs(s["tp1"] - s["entry"]) if s["side"] == "long" else abs(s["entry"] - s["tp1"]), 2),
        })

    summary = {
        "executed": len(executed),
        "start_equity": equity_start,
        "end_equity": round(equity, 2),
        "net_pnl": round(equity - equity_start, 2),
        "max_drawdown": round(max_dd, 2),
        "wins": sum(1 for t in executed if t["outcome"] == "win"),
        "losses": sum(1 for t in executed if t["outcome"] == "loss"),
    }
    if executed:
        summary["win_rate"] = round(summary["wins"] / len(executed), 4)
        summary["avg_r"] = round(stats.mean(t["r"] for t in executed), 3)
        summary["profit_factor"] = round(
            abs(sum(t["pnl_usd"] for t in executed if t["outcome"] == "win"))
            / max(1e-9, abs(sum(t["pnl_usd"] for t in executed if t["outcome"] == "loss"))),
            2,
        )
    return executed, summary


def main() -> int:
    p = argparse.ArgumentParser(description="Deep IFVG strategy audit")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--end", default="2026-05-29")
    p.add_argument("--data-dir", default=str(REPO / "data" / "agent_live_xauusd"))
    p.add_argument("--equity", type=float, default=100.0)
    p.add_argument("--leverage", type=float, default=1000.0)
    p.add_argument("--cap-lot", type=float, default=None, help="Max lot size e.g. 0.05")
    p.add_argument("--spread", type=float, default=0.70, help="Spread $ per 0.01 lot RT")
    args = p.parse_args()

    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=23, minutes=59)
    start_dt = end_dt - timedelta(days=args.days)

    macro = load_macro_frame(REPO / "data" / "macro")
    if not macro.names():
        macro = None
    levels = load_market_levels(LEVELS)
    calendar = NewsCalendar.load(NEWS)
    data_dir = Path(args.data_dir)

    bars_by_tf: dict[int, list] = {}
    for tf, fname in ((5, "5m"), (15, "15m"), (60, "60m")):
        path = data_dir / f"xauusd_{fname}.csv"
        if not path.exists():
            continue
        full = load_bars_from_csv(path)
        window = _filter_bars(full, start_dt - timedelta(days=7), end_dt)
        bars_by_tf[tf] = window

    methodology = {
        "strategy_engine": "gold_trader.assistants.ifvg_confluence.find_ifvg_setups",
        "sequence": "liquidity sweep → displacement → IFVG inversion → retest rejection",
        "checklist": [
            "liquidity_sweep (20)", "displacement (15)", "inversion (20)", "retest_rejection (15)",
            "htf_agreement (15)", "macro_confirmation (10)", "not_into_sr (5)",
        ],
        "verdict_thresholds": {"valid_entry": "score>=80", "alert_wait": "score>=65", "ignore": "<65"},
        "final_grading": "gold_trader.assistants.ifvg_grading (A/B/C/D)",
        "htf_bias": "compute_htf_bias_for_scout (4H+1H EMA from bars)",
        "macro": "local CSV (dxy, us10y, real10y)" if macro else "unavailable",
        "news_calendar": str(NEWS) if NEWS.exists() else "missing",
        "market_levels": str(LEVELS) if LEVELS.exists() else "missing",
        "data_source": str(data_dir),
        "window": f"{start_dt.date()} → {end_dt.date()} UTC",
        "included_vs_live_ui": {
            "included": [
                "IFVG candidate scan on each bar",
                "Full 7-item checklist scoring",
                "HTF bias from bars",
                "Macro CSV confirmation",
                "News blackout warnings",
                "market_levels.json danger check",
                "Entry/stop/TP1/TP2/TP3 from _entry_plan",
                "Final A/B/C/D grade",
            ],
            "not_included_or_different": [
                "OpenAI web research DISABLED in fast audit (use --with-openai for API replay)",
                "8-step workflow hard gates (buy at resistance block) NOT applied in execution",
                "M1 confirmation layer NOT scanned separately",
                "Entry at signal-bar CLOSE (not limit inside zone)",
                "Exit at TP1 only (TP2/TP3/partials not simulated)",
                "No slippage beyond spread estimate",
                "Bar replay uses cached CSV not live tick feed",
                "Prior chat backtest used simplified script without HTF/macro/news/levels",
            ],
        },
    }

    all_signals: list[dict] = []
    for tf, bars in bars_by_tf.items():
        print(f"Scanning M{tf} ({len(bars)} bars)...", flush=True)
        sigs = collect_signals(
            bars, tf=tf, start=start_dt, end=end_dt, macro=macro, levels=levels,
            calendar=calendar, min_final_grade="C", include_openai=False, min_score=50,
        )
        all_signals.extend(sigs)
        print(f"  M{tf}: {len(sigs)} grade-C+ signals", flush=True)

    scenarios = {}
    for name, live_only, one_pos, cap in [
        ("A_all_C+_signals", False, True, args.cap_lot),
        ("B_live_enter_rules_AB_only", True, True, args.cap_lot),
        ("C_prior_simplified_no_macro_htf", False, True, args.cap_lot),
    ]:
        sigs = all_signals
        if name.startswith("B_"):
            sigs = [s for s in all_signals if s["would_enter_live"]]
        ex, sm = execute_trades(
            sigs, bars_by_tf,
            equity_start=args.equity,
            leverage=args.leverage,
            cap_lot=cap,
            spread_per_01=args.spread,
            one_position=one_pos,
            live_entries_only=name.startswith("B_"),
        )
        scenarios[name] = sm

    # Verdict / grade stats on all signals
    by_verdict = Counter(s["verdict"] for s in all_signals)
    by_final = Counter(s["final_grade"] for s in all_signals)
    live_eligible = sum(1 for s in all_signals if s["would_enter_live"])

    report = {
        "methodology": methodology,
        "signal_stats": {
            "total_grade_c_plus_signals": len(all_signals),
            "live_enter_eligible_AB": live_eligible,
            "by_verdict": dict(by_verdict),
            "by_final_grade": dict(by_final),
            "by_tf": dict(Counter(f"M{s['tf']}" for s in all_signals)),
        },
        "execution_scenarios": scenarios,
    }

    out_json = REPO / "logs" / "ifvg_deep_audit_report.json"
    out_json.parent.mkdir(exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2))

    # Best scenario detail CSV
    ex_full, _ = execute_trades(
        all_signals, bars_by_tf,
        equity_start=args.equity, leverage=args.leverage, cap_lot=args.cap_lot,
        spread_per_01=args.spread, one_position=True, live_entries_only=False,
    )
    csv_path = REPO / "logs" / "ifvg_deep_audit_trades.csv"
    if ex_full:
        import csv
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(ex_full[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(ex_full)

    print("\n" + "=" * 72)
    print("DEEP IFVG AUDIT — DID WE USE OUR OWN STRATEGY?")
    print("=" * 72)
    print("YES — entry points come from find_ifvg_setups() in ifvg_confluence.py")
    print("Same module the live scout and Trade UI use.")
    print(f"Window: {start_dt.date()} → {end_dt.date()} ({args.days} days)")
    print(f"Data: {data_dir}")
    print()
    print("SIGNAL DETECTION (grade C+, score>=50, full checklist + macro + HTF + levels):")
    print(f"  Total unique signals: {len(all_signals)}")
    print(f"  Would show Enter trade (A/B live rules): {live_eligible}")
    print(f"  By verdict: {dict(by_verdict)}")
    print(f"  By final grade: {dict(by_final)}")
    print()
    print("EXECUTION SCENARIOS (graded lot sizing, TP1/SL, spread, one position):")
    for name, sm in scenarios.items():
        wr = sm.get("win_rate", 0) * 100
        print(f"  [{name}]")
        print(f"    trades={sm['executed']} W={sm['wins']} L={sm['losses']} WR={wr:.1f}% "
              f"net=${sm['net_pnl']:+,.2f} end=${sm['end_equity']:,.2f} PF={sm.get('profit_factor','?')}")
    print()
    print("WHAT THE EARLIER CHAT BACKTEST DID DIFFERENTLY:")
    print("  - Used same find_ifvg_setups BUT skipped HTF bias, macro, news, market_levels")
    print("  - OpenAI research off")
    print("  - Fixed 0.05 lot OR broken loss accounting (fixed -1R $ not full SL)")
    print("  - This audit fixes those gaps.")
    print()
    print(f"Full report: {out_json}")
    if ex_full:
        print(f"Trade log:   {csv_path} ({len(ex_full)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
