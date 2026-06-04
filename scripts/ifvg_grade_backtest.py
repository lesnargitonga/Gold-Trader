#!/usr/bin/env python3
"""Backtest IFVG setups that reached grade C+ over a recent window.

Simulates entering every qualifying setup on M5/M15/M60 with XM-style sizing:
$100 equity, 1000:1 leverage, risk from grade (A=1%, B=0.5%, C=0.25%).
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.assistants.ifvg_confluence import (  # noqa: E402
    IFVGAssistantConfig,
    find_ifvg_setups,
    setup_to_dict,
)
from gold_trader.assistants.ifvg_grading import RISK_BY_LETTER, letter_from_score  # noqa: E402
from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.models import Side  # noqa: E402

CONTRACT_SIZE = 100.0  # XAUUSD oz per lot (XM standard)
MIN_LOT = 0.01
VOLUME_STEP = 0.01


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _filter_bars(bars, start: datetime, end: datetime):
    return [b for b in bars if start <= b.timestamp <= end]


def _round_lots(lots: float) -> float:
    return math.floor(lots / VOLUME_STEP) * VOLUME_STEP


def _size_lots(
    *,
    entry: float,
    stop: float,
    risk_pct: float,
    equity: float,
    leverage: float,
) -> float | None:
    stop_dist = abs(entry - stop)
    if stop_dist <= 0:
        return None
    risk_dollars = equity * risk_pct
    loss_per_lot = stop_dist * CONTRACT_SIZE
    lots = risk_dollars / loss_per_lot
    max_lots = (equity * leverage) / (CONTRACT_SIZE * entry)
    lots = min(lots, max_lots)
    lots = _round_lots(lots)
    if lots < MIN_LOT:
        lots = MIN_LOT
    margin = (lots * CONTRACT_SIZE * entry) / leverage
    if margin > equity * 1.05:
        return None
    return lots


def _simulate(
    bars,
    start_idx: int,
    *,
    side: str,
    entry: float,
    stop: float,
    tp1: float,
    max_bars: int,
) -> tuple[str, float | None]:
    is_long = side.lower() == "long"
    end = min(len(bars), start_idx + 1 + max_bars)
    for i in range(start_idx + 1, end):
        bar = bars[i]
        if is_long:
            stop_hit = bar.low <= stop
            tp_hit = bar.high >= tp1
        else:
            stop_hit = bar.high >= stop
            tp_hit = bar.low <= tp1
        if stop_hit and tp_hit:
            return "loss", stop
        if stop_hit:
            return "loss", stop
        if tp_hit:
            return "win", tp1
    return "open", None


def _style(tf: int) -> str:
    if tf <= 5:
        return "scalp"
    if tf <= 15:
        return "intraday"
    if tf <= 60:
        return "swing"
    return "position"


def _scan_tf(
    bars,
    *,
    tf: int,
    start: datetime,
    end: datetime,
    macro_frame,
    min_grade: str,
    equity: float,
    leverage: float,
    min_score: int,
    no_overlap: bool,
) -> list[dict]:
    grade_ok = {"A": {"A", "B", "C"}, "B": {"A", "B"}, "C": {"A", "B", "C"}}[min_grade]
    cfg = IFVGAssistantConfig()
    warmup = max(cfg.atr_period + 5, 80)
    seen: set[tuple] = set()
    trades: list[dict] = []
    busy_until = -1

    for i in range(warmup, len(bars)):
        ts = bars[i].timestamp
        if ts < start or ts > end:
            continue
        if no_overlap and i <= busy_until:
            continue
        setups = find_ifvg_setups(bars, index=i, macro_frame=macro_frame, force_external_research=False)
        if not setups:
            continue
        best = setups[0]
        d = setup_to_dict(best, timeframe_minutes=tf)
        grading = d.get("grading") or {}
        letter = str(grading.get("letter") or letter_from_score(best.score))
        if letter not in grade_ok or best.score < min_score:
            continue
        c = best.candidate
        key = (tf, best.candidate.side.value, c.formation_idx, c.inversion_idx, c.signal_idx)
        if key in seen:
            continue
        seen.add(key)

        plan = best.plan
        entry = plan.entry
        stop = plan.stop
        tp1 = plan.tp1
        risk_pct = float(grading.get("suggested_risk_pct") or RISK_BY_LETTER.get(letter, 0.0))
        lots = _size_lots(entry=entry, stop=stop, risk_pct=risk_pct, equity=equity, leverage=leverage)
        if lots is None:
            continue

        horizon = {5: 576, 15: 384, 60: 168}.get(tf, 384)  # ~2d / ~4d / ~7d in bars
        outcome, exit_px = _simulate(
            bars, i, side=best.candidate.side.value, entry=entry, stop=stop, tp1=tp1, max_bars=horizon,
        )
        if outcome == "open":
            continue

        exit_idx = i + 1
        for j in range(i + 1, min(len(bars), i + 1 + horizon)):
            bar = bars[j]
            is_long = best.candidate.side is Side.LONG
            if is_long:
                if bar.low <= stop or bar.high >= tp1:
                    exit_idx = j
                    break
            elif bar.high >= stop or bar.low <= tp1:
                exit_idx = j
                break
        if no_overlap:
            busy_until = exit_idx

        is_long = best.candidate.side is Side.LONG
        if outcome == "win":
            move = (exit_px - entry) if is_long else (entry - exit_px)
            pnl = move * CONTRACT_SIZE * lots
            r_mult = move / abs(entry - stop) if abs(entry - stop) > 0 else 0
        else:
            pnl = -(equity * risk_pct)
            r_mult = -1.0

        trades.append({
            "time": ts.isoformat(),
            "exit_time": bars[exit_idx].timestamp.isoformat(),
            "tf": tf,
            "style": _style(tf),
            "side": best.candidate.side.value,
            "grade": letter,
            "tech_score": best.score,
            "final_score": grading.get("final_score", best.score),
            "verdict": best.verdict,
            "entry": entry,
            "stop": stop,
            "tp1": tp1,
            "lots": lots,
            "risk_pct": risk_pct,
            "outcome": outcome,
            "pnl_usd": round(pnl, 2),
            "r": round(r_mult, 2),
        })
    return trades


def main() -> int:
    parser = argparse.ArgumentParser(description="IFVG grade C+ recent backtest")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--end", default="2026-05-30", help="End date YYYY-MM-DD (UTC)")
    parser.add_argument("--equity", type=float, default=100.0)
    parser.add_argument("--leverage", type=float, default=1000.0)
    parser.add_argument("--min-grade", default="C", choices=["A", "B", "C"])
    parser.add_argument("--data-dir", default=str(REPO / "data" / "agent_live_xauusd"))
    parser.add_argument("--no-overlap", action="store_true", help="One trade at a time per timeframe")
    parser.add_argument("--one-position", action="store_true", help="Only one open trade across all TFs")
    args = parser.parse_args()

    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=23, minutes=59)
    start_dt = end_dt - timedelta(days=args.days)
    min_score = {"A": 80, "B": 65, "C": 50}[args.min_grade]

    data_dir = Path(args.data_dir)
    macro = load_macro_frame(REPO / "data" / "macro")
    if not macro.names():
        macro = None

    all_trades: list[dict] = []
    global_busy_until: dict[int, int] = {}  # bar index in unified timeline - skip for one-position

    # Pre-load all TFs
    tf_data: list[tuple[int, list]] = []
    for tf, fname in ((5, "5m"), (15, "15m"), (60, "60m")):
        preferred = data_dir / f"xauusd_{fname}.csv"
        path = preferred if preferred.exists() else None
        if path is None:
            matches = sorted(data_dir.glob(f"*_{fname}.csv"))
            path = matches[-1] if matches else None
        if path is None:
            print(f"no CSV for M{tf} in {data_dir}")
            continue
        bars = load_bars_from_csv(path)
        window = _filter_bars(bars, start_dt - timedelta(days=5), end_dt)
        if len(window) < 100:
            print(f"M{tf}: insufficient bars ({len(window)})")
            continue
        tf_data.append((tf, window))

    if args.one_position:
        # Merge signals chronologically, one position at a time
        candidates: list[tuple[datetime, int, int, dict]] = []
        for tf, window in tf_data:
            raw = _scan_tf(
                window,
                tf=tf,
                start=start_dt,
                end=end_dt,
                macro_frame=macro,
                min_grade=args.min_grade,
                equity=args.equity,
                leverage=args.leverage,
                min_score=min_score,
                no_overlap=False,
            )
            for t in raw:
                candidates.append((_parse_ts(t["time"]), tf, 0, t))
        candidates.sort(key=lambda x: x[0])
        busy_until_time: datetime | None = None
        for ts, tf, _, t in candidates:
            if busy_until_time and ts <= busy_until_time:
                continue
            all_trades.append(t)
            busy_until_time = _parse_ts(t["exit_time"])
    else:
        for tf, window in tf_data:
            trades = _scan_tf(
                window,
                tf=tf,
                start=start_dt,
                end=end_dt,
                macro_frame=macro,
                min_grade=args.min_grade,
                equity=args.equity,
                leverage=args.leverage,
                min_score=min_score,
                no_overlap=args.no_overlap,
            )
            all_trades.extend(trades)

    all_trades.sort(key=lambda t: t["time"])
    wins = [t for t in all_trades if t["outcome"] == "win"]
    losses = [t for t in all_trades if t["outcome"] == "loss"]
    total_pnl = sum(t["pnl_usd"] for t in all_trades)
    total_r = sum(t["r"] for t in all_trades)

    print(f"IFVG backtest · last {args.days} days · grade {args.min_grade}+")
    print(f"Window: {start_dt.date()} → {end_dt.date()} UTC")
    print(f"Account: ${args.equity:.0f} · leverage {args.leverage:.0f}:1 · XM standard (0.01 lot min, 100 oz/lot)")
    mode = "one position globally" if args.one_position else ("one per TF" if args.no_overlap else "every unique signal")
    print(f"Overlap: {mode}")
    print()
    print(f"Trades taken: {len(all_trades)}")
    print(f"Wins: {len(wins)} ({100*len(wins)/len(all_trades):.1f}%)" if all_trades else "Wins: 0")
    print(f"Losses: {len(losses)}")
    print(f"Win rate (TP1 before stop): {len(wins)/len(all_trades):.1%}" if all_trades else "Win rate: n/a")
    print(f"Total PnL: ${total_pnl:+.2f}")
    print(f"Total R: {total_r:+.2f}R")
    print(f"Ending equity (sequential 1% risk stack approx): ${args.equity + total_pnl:.2f}")
    print()

    by_grade: dict[str, list] = {}
    by_style: dict[str, list] = {}
    for t in all_trades:
        by_grade.setdefault(t["grade"], []).append(t)
        by_style.setdefault(t["style"], []).append(t)

    print("By grade:")
    for g in ("A", "B", "C"):
        rows = by_grade.get(g, [])
        if not rows:
            continue
        w = sum(1 for r in rows if r["outcome"] == "win")
        pnl = sum(r["pnl_usd"] for r in rows)
        print(f"  {g}: n={len(rows)} wins={w} wr={w/len(rows):.1%} pnl=${pnl:+.2f}")

    print("By style:")
    for s in ("scalp", "intraday", "swing"):
        rows = by_style.get(s, [])
        if not rows:
            continue
        w = sum(1 for r in rows if r["outcome"] == "win")
        pnl = sum(r["pnl_usd"] for r in rows)
        print(f"  {s}: n={len(rows)} wins={w} wr={w/len(rows):.1%} pnl=${pnl:+.2f}")

    print()
    print("Last 15 trades:")
    for t in all_trades[-15:]:
        print(
            f"  {t['time'][:16]} M{t['tf']} {t['side']} grade={t['grade']} "
            f"{t['outcome']} ${t['pnl_usd']:+.2f} ({t['r']:+.1f}R) entry={t['entry']:.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
