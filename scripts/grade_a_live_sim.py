#!/usr/bin/env python3
"""30-day Grade A live-profile simulation — fixed lot, production can_enter gates.

Replicates build_approval_brief / evaluate_live_sentiment from ifvg_scout + ifvg_workflow:
  - Grade A only, verdict valid_entry|alert_wait, score >= 65
  - workflow_ready, no sentiment blockers (mixed bearish bias preferred)
  - Workflow hard-fail on steps 1 and 3
  - M15 + M60 IFVG scan only (no M5 scout)
"""
from __future__ import annotations

import argparse
import json
import math
import statistics as stats
import sys
from collections import Counter
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
from gold_trader.assistants.ifvg_scout import build_approval_brief  # noqa: E402
from gold_trader.assistants.ifvg_workflow import (  # noqa: E402
    alignment_from_bars,
    build_workflow_context,
    compute_htf_bias_for_scout,
    evaluate_live_sentiment,
    macro_regime_for_side,
)
from gold_trader.calendar import NewsCalendar  # noqa: E402
from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.models import MarketBar  # noqa: E402
from gold_trader.research.realtime_research import load_openai_research_config  # noqa: E402

CONTRACT = 100.0
MIN_LOT = 0.01
LEVELS = REPO / "config" / "market_levels.json"
NEWS = REPO / "data" / "macro" / "news_calendar.csv"
OPENAI_CFG = REPO / "config" / "openai_research.json"
SCAN_TFS = (15, 60)
ALIGNMENT_TFS = (15, 60, 240)


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _filter_bars(bars: list[MarketBar], start: datetime, end: datetime) -> list[MarketBar]:
    return [b for b in bars if start <= b.timestamp <= end]


def _slice_up_to(bars: list[MarketBar], ts: datetime) -> list[MarketBar]:
    out: list[MarketBar] = []
    for b in bars:
        if b.timestamp <= ts:
            out.append(b)
        else:
            break
    return out


def _margin_ok(lots: float, entry: float, equity: float, leverage: float) -> tuple[bool, float]:
    margin = (lots * CONTRACT * entry) / leverage
    return margin <= equity * 1.05, margin


def _simulate(
    bars: list[MarketBar],
    idx: int,
    *,
    side: str,
    entry: float,
    stop: float,
    tp: float,
    max_bars: int,
) -> tuple[str, float | None, int]:
    """Bar-by-bar SL/TP; exit at TP1 (engine plan tp1)."""
    is_long = side == "long"
    end = min(len(bars), idx + 1 + max_bars)
    for j in range(idx + 1, end):
        bar = bars[j]
        if is_long:
            if bar.low <= stop:
                return "loss", stop, j
            if bar.high >= tp:
                return "win", tp, j
        else:
            if bar.high >= stop:
                return "loss", stop, j
            if bar.low <= tp:
                return "win", tp, j
    return "open", None, j if end > idx + 1 else idx


def collect_grade_a_signals(
    all_bars: dict[int, list[MarketBar]],
    *,
    start: datetime,
    end: datetime,
    macro,
    levels,
    calendar,
) -> list[dict]:
    from gold_trader.research.realtime_research import OpenAIResearchConfig

    cfg = IFVGAssistantConfig()
    warmup = max(cfg.atr_period + 5, 80)
    research_cfg = load_openai_research_config(OPENAI_CFG)
    research_cfg = OpenAIResearchConfig(enabled=False, mode="off")

    seen: set[tuple] = set()
    out: list[dict] = []

    for tf in SCAN_TFS:
        bars = all_bars.get(tf)
        if not bars:
            continue
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
            grading = d.get("grading") or {}

            c = best.candidate
            key = (tf, c.side.value, c.formation_idx, c.inversion_idx, c.signal_idx)
            if key in seen:
                continue
            seen.add(key)

            bars_by_tf: dict[int, list[MarketBar]] = {}
            for t in (*SCAN_TFS, 5, *ALIGNMENT_TFS):
                full = all_bars.get(t)
                if full:
                    sliced = _slice_up_to(full, ts)
                    if len(sliced) >= 50 or t in (5, 1):
                        bars_by_tf[t] = sliced

            primary = bars_by_tf.get(tf) or _slice_up_to(bars, ts)
            price = primary[-1].close if primary else best.plan.entry
            workflow = build_workflow_context(
                d,
                primary_bars=primary,
                current_price=price,
                bars_by_tf=bars_by_tf,
                market_levels=levels,
            )
            side = str(d.get("side") or "")
            workflow["live_sentiment"] = evaluate_live_sentiment(
                side=side,
                alignment=alignment_from_bars(bars_by_tf),
                macro_regime=macro_regime_for_side(side, macro, ts),
            )
            d["workflow"] = workflow
            brief = build_approval_brief(d, workflow)

            plan = best.plan
            out.append({
                "time": ts.isoformat(),
                "tf": tf,
                "side": c.side.value,
                "final_grade": str(grading.get("letter") or "D"),
                "tech_score": best.score,
                "verdict": best.verdict,
                "can_enter": bool(brief.get("can_enter")),
                "workflow_ready": bool(workflow.get("workflow_ready")),
                "alignment": (workflow.get("live_sentiment") or {}).get("alignment"),
                "macro_regime": (workflow.get("live_sentiment") or {}).get("macro_regime"),
                "sentiment_blockers": list((workflow.get("live_sentiment") or {}).get("blockers") or []),
                "brief_blockers": list(brief.get("blockers") or [])[:4],
                "entry": plan.entry,
                "stop": plan.stop,
                "tp1": plan.tp1,
                "tp2": plan.tp2,
                "bar_idx": i,
            })
    return out


def execute_fixed_lot(
    signals: list[dict],
    bars_by_tf: dict[int, list[MarketBar]],
    *,
    equity_start: float,
    fixed_lots: float,
    leverage: float,
    spread_per_01: float,
    one_position: bool,
    can_enter_only: bool,
) -> tuple[list[dict], dict]:
    rows = [s for s in signals if (not can_enter_only or s["can_enter"])]
    rows.sort(key=lambda r: r["time"])
    equity = equity_start
    peak = equity
    max_dd = 0.0
    busy_until: datetime | None = None
    executed: list[dict] = []
    margin_rejects = 0
    blown = False

    for s in rows:
        if blown:
            break
        ts = _parse_ts(s["time"])
        if one_position and busy_until and ts <= busy_until:
            continue

        tf = int(s["tf"])
        bars = bars_by_tf[tf]
        idx = s["bar_idx"]
        entry = float(s["entry"])
        lots = fixed_lots

        ok, margin_req = _margin_ok(lots, entry, equity, leverage)
        if not ok:
            margin_rejects += 1
            continue

        horizon = {15: 384, 60: 168}.get(tf, 384)
        outcome, exit_px, exit_idx = _simulate(
            bars,
            idx,
            side=s["side"],
            entry=entry,
            stop=float(s["stop"]),
            tp=float(s["tp1"]),
            max_bars=horizon,
        )
        if outcome == "open":
            continue

        spread = spread_per_01 * (lots / MIN_LOT)
        sl_dist = abs(entry - float(s["stop"]))
        if outcome == "win":
            move = (exit_px - entry) if s["side"] == "long" else (entry - exit_px)
            pnl = move * CONTRACT * lots - spread
            r_mult = move / sl_dist if sl_dist else 0.0
        else:
            pnl = -sl_dist * CONTRACT * lots - spread
            r_mult = -1.0

        equity += pnl
        if equity <= 0:
            equity = 0.0
            blown = True
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        exit_ts = bars[exit_idx].timestamp.isoformat()
        if one_position:
            busy_until = _parse_ts(exit_ts)

        executed.append({
            **s,
            "lots": lots,
            "margin_usd": round(margin_req, 2),
            "outcome": outcome,
            "exit_target": "tp1",
            "pnl_usd": round(pnl, 2),
            "r": round(r_mult, 3),
            "spread_usd": round(spread, 2),
            "exit_time": exit_ts,
            "equity_after": round(equity, 2),
        })

    summary: dict = {
        "executed": len(executed),
        "margin_rejects": margin_rejects,
        "start_equity": equity_start,
        "end_equity": round(equity, 2),
        "net_pnl": round(equity - equity_start, 2),
        "max_drawdown": round(max_dd, 2),
        "account_blown": blown or equity <= 0,
        "wins": sum(1 for t in executed if t["outcome"] == "win"),
        "losses": sum(1 for t in executed if t["outcome"] == "loss"),
        "fixed_lots": fixed_lots,
        "spread_per_005_rt": round(spread_per_01 * (fixed_lots / MIN_LOT), 2),
        "exit_rule": "honest SL vs plan TP1 (ifvg entry_plan.tp1)",
    }
    if executed:
        summary["win_rate"] = round(summary["wins"] / len(executed), 4)
        summary["avg_r"] = round(stats.mean(t["r"] for t in executed), 3)
        gross_win = sum(t["pnl_usd"] for t in executed if t["outcome"] == "win")
        gross_loss = abs(sum(t["pnl_usd"] for t in executed if t["outcome"] == "loss"))
        summary["profit_factor"] = round(gross_win / max(1e-9, gross_loss), 2)
    return executed, summary


def main() -> int:
    p = argparse.ArgumentParser(description="Grade A live-profile 30d sim (fixed lot)")
    p.add_argument("--start", default="2026-04-29")
    p.add_argument("--end", default="2026-05-29")
    p.add_argument("--data-dir", default=str(REPO / "data" / "agent_live_xauusd"))
    p.add_argument("--equity", type=float, default=100.0)
    p.add_argument("--lots", type=float, default=0.05)
    p.add_argument("--leverage", type=float, default=1000.0)
    p.add_argument("--spread", type=float, default=0.70, help="Round-trip spread $ per 0.01 lot")
    args = p.parse_args()

    start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(
        hours=23, minutes=59
    )

    macro = load_macro_frame(REPO / "data" / "macro")
    if not macro.names():
        macro = None
    levels = load_market_levels(LEVELS)
    calendar = NewsCalendar.load(NEWS)
    data_dir = Path(args.data_dir)

    all_bars: dict[int, list[MarketBar]] = {}
    for tf, fname in ((5, "5m"), (15, "15m"), (60, "60m"), (240, "240m")):
        path = data_dir / f"xauusd_{fname}.csv"
        if not path.exists():
            continue
        full = load_bars_from_csv(path)
        all_bars[tf] = _filter_bars(full, start_dt - timedelta(days=14), end_dt)

    print(f"Grade A live sim · {start_dt.date()} → {end_dt.date()}", flush=True)
    print(f"Equity ${args.equity} · fixed {args.lots} lot · spread ${args.spread}/0.01 RT", flush=True)

    signals = collect_grade_a_signals(
        all_bars,
        start=start_dt,
        end=end_dt,
        macro=macro,
        levels=levels,
        calendar=calendar,
    )

    grade_a = [s for s in signals if s["final_grade"] == "A"]
    can_enter = [s for s in signals if s["can_enter"]]

    seq_trades, seq_sum = execute_fixed_lot(
        signals,
        all_bars,
        equity_start=args.equity,
        fixed_lots=args.lots,
        leverage=args.leverage,
        spread_per_01=args.spread,
        one_position=True,
        can_enter_only=True,
    )
    par_trades, par_sum = execute_fixed_lot(
        signals,
        all_bars,
        equity_start=args.equity,
        fixed_lots=args.lots,
        leverage=args.leverage,
        spread_per_01=args.spread,
        one_position=False,
        can_enter_only=True,
    )

    report = {
        "window": {"start": args.start, "end": args.end},
        "profile": {
            "grade": "A only via build_approval_brief",
            "timeframes": list(SCAN_TFS),
            "alignment_gate": "mixed bearish bias (blockers on other alignments)",
            "macro_gate": "macro_regime=mixed required (hard block aligned/opposed/unknown)",
            "workflow_ready": True,
            "openai_research": False,
        },
        "signal_stats": {
            "all_ifvg_signals_m15_m60": len(signals),
            "grade_a": len(grade_a),
            "can_enter_live": len(can_enter),
            "by_tf": dict(Counter(f"M{s['tf']}" for s in can_enter)),
            "by_alignment": dict(Counter(s.get("alignment") for s in can_enter)),
            "by_macro_regime": dict(Counter(s.get("macro_regime") for s in can_enter)),
        },
        "sequential_one_position": seq_sum,
        "parallel_all_can_enter": par_sum,
        "trades": seq_trades,
    }

    out = REPO / "logs" / "grade_a_live_sim_30d.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2))

    print()
    print("=" * 72)
    print("GRADE A LIVE PROFILE — 30-DAY FIXED 0.05 LOT @ $100")
    print("=" * 72)
    print(f"Signals M15+M60: {len(signals)} · Grade A: {len(grade_a)} · can_enter: {len(can_enter)}")
    print(f"  Alignment mix: {dict(Counter(s.get('alignment') for s in can_enter))}")
    print()
    print("SEQUENTIAL (one position, realistic):")
    wr = (seq_sum.get("win_rate") or 0) * 100
    print(
        f"  Trades {seq_sum['executed']} · W/L {seq_sum['wins']}/{seq_sum['losses']} · WR {wr:.1f}%"
    )
    print(
        f"  Start ${seq_sum['start_equity']:.2f} → End ${seq_sum['end_equity']:.2f} "
        f"(net ${seq_sum['net_pnl']:+.2f})"
    )
    print(f"  Max DD ${seq_sum['max_drawdown']:.2f} · margin skips {seq_sum['margin_rejects']}")
    print(f"  Account blown: {seq_sum['account_blown']}")
    print()
    print("PARALLEL (all can_enter fired, overlap):")
    pwr = (par_sum.get("win_rate") or 0) * 100
    print(
        f"  Trades {par_sum['executed']} · net ${par_sum['net_pnl']:+.2f} · end ${par_sum['end_equity']:.2f} · WR {pwr:.1f}%"
    )
    print(f"\nReport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
