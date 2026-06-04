#!/usr/bin/env python3
"""IFVG execution geometry audit — zone SL vs structural SL, partial/runner, sentiment slices.

Hypothesis: IFVG edge improves with zone-end SL (not wide structural sweep stop),
minimum 1R TP1, partial/runner exits, and sentiment metadata for slice analysis.

Entry: signal-bar close (retest rejection at close), no hindsight.
Grades A/B/C included in main run; sentiment tagged not filtered.
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
from typing import Any, Sequence

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.assistants.ifvg_confluence import (  # noqa: E402
    IFVGAssistantConfig,
    IFVGCandidate,
    MarketLevel,
    find_ifvg_setups,
    load_market_levels,
    setup_to_dict,
)
from gold_trader.assistants.ifvg_grading import compute_setup_grading  # noqa: E402
from gold_trader.assistants.ifvg_workflow import (  # noqa: E402
    alignment_from_bars,
    compute_htf_bias_for_scout,
    macro_regime_for_side,
)
from gold_trader.calendar import NewsCalendar  # noqa: E402
from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.models import MarketBar, Side  # noqa: E402
from gold_trader.research.realtime_research import (  # noqa: E402
    OpenAIResearchConfig,
    RealtimeResearchResult,
    load_openai_research_config,
)
from gold_trader.strategies.filters import session_of  # noqa: E402

CONTRACT = 100.0
MIN_LOT = 0.01
SPREAD_RT = 0.70
DEFAULT_RISK_USD = 30.0
LEVELS = REPO / "config" / "market_levels.json"
NEWS = REPO / "data" / "macro" / "news_calendar.csv"
OPENAI_CFG = REPO / "config" / "openai_research.json"
OPENAI_CACHE = REPO / "data/cache/openai_market_research.json"
SCAN_TFS = (15, 60)
ALIGNMENT_TFS = (15, 60, 240)
MODELS = (
    "IFVG_ZONE_SL_1R_2R",
    "IFVG_ZONE_SL_PARTIAL_RUNNER",
    "STRUCTURAL_SL_MICRO_TP_BASELINE",
    "STRUCTURAL_SL_2R",
)
TRADE_COLUMNS = [
    "timestamp", "timeframe", "side", "zone_low", "zone_high", "entry", "sl",
    "tp1", "tp2", "tp3", "model", "risk_points", "reward_tp1_points", "rr_tp1",
    "spread_cost", "result", "pnl_r", "max_favorable_excursion", "max_adverse_excursion",
    "htf_bias", "alignment", "macro_regime", "dxy_bias", "us10y_bias", "news_risk",
    "nearest_market_level", "level_distance", "session", "grade", "checklist_pass",
    "had_sweep", "openai_status", "pnl_usd_100acct",
]

MACRO_NEUTRAL_BAND = 0.05


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


def _atr(bars: list[MarketBar], index: int, period: int) -> float:
    if index <= 0:
        return 0.0
    start = max(1, index - period + 1)
    trs = [bars[i].true_range(bars[i - 1].close) for i in range(start, index + 1)]
    return sum(trs) / len(trs) if trs else 0.0


def _nearest_level(levels: Sequence[MarketLevel], price: float, side: Side) -> float | None:
    if side is Side.LONG:
        above = [lv.price for lv in levels if lv.price > price]
        return min(above) if above else None
    below = [lv.price for lv in levels if lv.price < price]
    return max(below) if below else None


def zone_sl_plan(
    bars: list[MarketBar],
    candidate: IFVGCandidate,
    atr: float,
    market_levels: list[MarketLevel],
    cfg: IFVGAssistantConfig,
) -> dict[str, float]:
    """Zone-end SL just outside IFVG gap + buffer — NOT sweep structural stop."""
    entry = bars[candidate.signal_idx].close
    buffer = max(cfg.stop_buffer_atr * atr, 0.1)
    if candidate.side is Side.SHORT:
        stop = candidate.gap_top + buffer
        risk = max(stop - entry, buffer)
        tp1 = entry - risk
        tp2 = entry - 2 * risk
        tp3 = _nearest_level(market_levels, entry, Side.SHORT) or (entry - 3 * risk)
    else:
        stop = candidate.gap_bot - buffer
        risk = max(entry - stop, buffer)
        tp1 = entry + risk
        tp2 = entry + 2 * risk
        tp3 = _nearest_level(market_levels, entry, Side.LONG) or (entry + 3 * risk)
    return {
        "entry": entry,
        "sl": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "risk_points": risk,
    }


def structural_2r_plan(structural: dict[str, float]) -> dict[str, float]:
    """Structural SL from engine plan; TP1 forced to minimum 2R."""
    entry = structural["entry"]
    stop = structural["sl"]
    risk = max(abs(entry - stop), 0.1)
    if structural.get("side") == "long" or entry > stop:
        tp1 = entry + 2 * risk
    else:
        tp1 = entry - 2 * risk
    return {
        **structural,
        "tp1": tp1,
        "risk_points": risk,
    }


def _instrument_bias(side: Side, delta: float | None) -> str:
    if delta is None:
        return "unknown"
    if abs(delta) < MACRO_NEUTRAL_BAND:
        return "neutral"
    if side is Side.LONG:
        return "supports_buy" if delta <= 0 else "supports_sell"
    return "supports_sell" if delta >= 0 else "supports_buy"


def _openai_cache_key(
    *,
    side: str,
    entry: float,
    zone_lo: float,
    zone_hi: float,
    at: datetime,
) -> str:
    hour_bucket = at.strftime("%Y%m%d%H")
    norm_side = "buy" if side.lower() in {"long", "buy"} else "sell"
    return "|".join([
        "XAUUSD",
        norm_side,
        f"{round(float(entry), 1):.1f}",
        f"{round(float(zone_lo), 1):.1f}",
        f"{round(float(zone_hi), 1):.1f}",
        hour_bucket,
    ])


def _load_openai_cache() -> dict[str, Any]:
    if not OPENAI_CACHE.exists():
        return {"records": {}}
    try:
        data = json.loads(OPENAI_CACHE.read_text())
    except Exception:
        return {"records": {}}
    if not isinstance(data, dict):
        return {"records": {}}
    data.setdefault("records", {})
    return data


def _result_from_cache_record(record: dict[str, Any]) -> RealtimeResearchResult | None:
    result = record.get("result")
    if not isinstance(result, dict):
        return None
    from gold_trader.research.realtime_research import _coerce_result

    return _coerce_result(result, symbol=str(result.get("symbol") or "XAUUSD"))


def lookup_openai_cache(
    *,
    side: str,
    entry: float,
    zone_lo: float,
    zone_hi: float,
    signal_ts: datetime,
    cache: dict[str, Any],
) -> tuple[RealtimeResearchResult | None, str]:
    records = cache.get("records") or {}
    for delta_h in (0, -1, 1, -2, 2):
        at = signal_ts + timedelta(hours=delta_h)
        key = _openai_cache_key(side=side, entry=entry, zone_lo=zone_lo, zone_hi=zone_hi, at=at)
        rec = records.get(key)
        if isinstance(rec, dict):
            result = _result_from_cache_record(rec)
            if result is not None:
                return result, "cache_hit"
    return None, "research_unavailable_at_signal"


def _news_context(calendar: NewsCalendar, ts: datetime) -> str:
    blocked, event = calendar.is_blackout(ts, window_minutes=30)
    if blocked and event is not None:
        return f"blackout:{event.impact}"
    nearest = calendar.nearest(ts)
    if nearest is None:
        return "clear"
    mins = abs((nearest.timestamp - ts).total_seconds()) / 60.0
    if mins <= 120:
        return nearest.impact
    return "clear"


def _level_context(
    levels: list[MarketLevel],
    price: float,
) -> tuple[str, float]:
    if not levels:
        return "", 0.0
    nearest = min(levels, key=lambda lv: abs(lv.price - price))
    label = nearest.label or nearest.kind
    return label, round(abs(nearest.price - price), 2)


def _size_lots(entry: float, stop: float, risk_usd: float = DEFAULT_RISK_USD) -> float:
    sl = abs(entry - stop)
    if sl <= 0:
        return MIN_LOT
    lots = risk_usd / (sl * CONTRACT)
    return max(MIN_LOT, min(0.05, math.floor(lots / MIN_LOT) * MIN_LOT))


def _spread_r(entry: float, stop: float, lots: float, risk_usd: float = DEFAULT_RISK_USD) -> tuple[float, float]:
    risk_pts = abs(entry - stop)
    if risk_pts <= 0:
        return 0.0, 0.0
    spread_usd = SPREAD_RT * (lots / MIN_LOT)
    spread_r = spread_usd / risk_usd
    return round(spread_usd, 2), round(spread_r, 4)


def _track_excursions(
    bars: list[MarketBar],
    idx: int,
    *,
    side: str,
    entry: float,
    end_idx: int,
) -> tuple[float, float]:
    is_long = side == "long"
    mfe = 0.0
    mae = 0.0
    for j in range(idx + 1, min(end_idx + 1, len(bars))):
        bar = bars[j]
        if is_long:
            mfe = max(mfe, bar.high - entry)
            mae = max(mae, entry - bar.low)
        else:
            mfe = max(mfe, entry - bar.low)
            mae = max(mae, bar.high - entry)
    return round(mfe, 2), round(mae, 2)


def simulate_simple(
    bars: list[MarketBar],
    idx: int,
    *,
    side: str,
    entry: float,
    stop: float,
    target: float,
    max_bars: int,
) -> dict[str, Any]:
    is_long = side == "long"
    end = min(len(bars), idx + 1 + max_bars)
    exit_idx = idx
    for j in range(idx + 1, end):
        bar = bars[j]
        exit_idx = j
        if is_long:
            if bar.low <= stop:
                mfe, mae = _track_excursions(bars, idx, side=side, entry=entry, end_idx=j)
                return {"result": "loss", "exit_px": stop, "exit_idx": j, "gross_r": -1.0, "mfe": mfe, "mae": mae}
            if bar.high >= target:
                move = target - entry
                gross_r = move / abs(entry - stop)
                mfe, mae = _track_excursions(bars, idx, side=side, entry=entry, end_idx=j)
                return {"result": "win", "exit_px": target, "exit_idx": j, "gross_r": gross_r, "mfe": mfe, "mae": mae}
        else:
            if bar.high >= stop:
                mfe, mae = _track_excursions(bars, idx, side=side, entry=entry, end_idx=j)
                return {"result": "loss", "exit_px": stop, "exit_idx": j, "gross_r": -1.0, "mfe": mfe, "mae": mae}
            if bar.low <= target:
                move = entry - target
                gross_r = move / abs(entry - stop)
                mfe, mae = _track_excursions(bars, idx, side=side, entry=entry, end_idx=j)
                return {"result": "win", "exit_px": target, "exit_idx": j, "gross_r": gross_r, "mfe": mfe, "mae": mae}
    mfe, mae = _track_excursions(bars, idx, side=side, entry=entry, end_idx=exit_idx)
    return {"result": "open", "exit_px": None, "exit_idx": exit_idx, "gross_r": 0.0, "mfe": mfe, "mae": mae}


def simulate_partial_runner(
    bars: list[MarketBar],
    idx: int,
    *,
    side: str,
    entry: float,
    stop: float,
    tp1: float,
    tp2: float,
    tp3: float,
    max_bars: int,
) -> dict[str, Any]:
    """50% at 1R, SL to BE, runner to first of 2R or TP3."""
    is_long = side == "long"
    risk = abs(entry - stop)
    if risk <= 0:
        return {"result": "open", "exit_px": None, "exit_idx": idx, "gross_r": 0.0, "mfe": 0.0, "mae": 0.0}

    if is_long:
        runner_targets = sorted([tp2, tp3])
        runner_target = runner_targets[0]
    else:
        runner_targets = sorted([tp2, tp3], reverse=True)
        runner_target = runner_targets[0]

    partial_done = False
    active_stop = stop
    end = min(len(bars), idx + 1 + max_bars)
    exit_idx = idx
    gross_r = 0.0

    for j in range(idx + 1, end):
        bar = bars[j]
        exit_idx = j
        if not partial_done:
            if is_long:
                if bar.low <= active_stop:
                    mfe, mae = _track_excursions(bars, idx, side=side, entry=entry, end_idx=j)
                    return {"result": "loss", "exit_px": active_stop, "exit_idx": j, "gross_r": -1.0, "mfe": mfe, "mae": mae}
                if bar.high >= tp1:
                    partial_done = True
                    gross_r += 0.5 * 1.0
                    active_stop = entry
            else:
                if bar.high >= active_stop:
                    mfe, mae = _track_excursions(bars, idx, side=side, entry=entry, end_idx=j)
                    return {"result": "loss", "exit_px": active_stop, "exit_idx": j, "gross_r": -1.0, "mfe": mfe, "mae": mae}
                if bar.low <= tp1:
                    partial_done = True
                    gross_r += 0.5 * 1.0
                    active_stop = entry

        if partial_done:
            if is_long:
                if bar.low <= active_stop:
                    result = "breakeven" if gross_r > 0 else "loss"
                    mfe, mae = _track_excursions(bars, idx, side=side, entry=entry, end_idx=j)
                    return {"result": result, "exit_px": active_stop, "exit_idx": j, "gross_r": gross_r, "mfe": mfe, "mae": mae}
                if bar.high >= runner_target:
                    runner_r = (runner_target - entry) / risk
                    gross_r += 0.5 * runner_r
                    mfe, mae = _track_excursions(bars, idx, side=side, entry=entry, end_idx=j)
                    return {"result": "win", "exit_px": runner_target, "exit_idx": j, "gross_r": gross_r, "mfe": mfe, "mae": mae}
            else:
                if bar.high >= active_stop:
                    result = "breakeven" if gross_r > 0 else "loss"
                    mfe, mae = _track_excursions(bars, idx, side=side, entry=entry, end_idx=j)
                    return {"result": result, "exit_px": active_stop, "exit_idx": j, "gross_r": gross_r, "mfe": mfe, "mae": mae}
                if bar.low <= runner_target:
                    runner_r = (entry - runner_target) / risk
                    gross_r += 0.5 * runner_r
                    mfe, mae = _track_excursions(bars, idx, side=side, entry=entry, end_idx=j)
                    return {"result": "win", "exit_px": runner_target, "exit_idx": j, "gross_r": gross_r, "mfe": mfe, "mae": mae}

    mfe, mae = _track_excursions(bars, idx, side=side, entry=entry, end_idx=exit_idx)
    return {"result": "open", "exit_px": None, "exit_idx": exit_idx, "gross_r": gross_r, "mfe": mfe, "mae": mae}


def collect_signals(
    all_bars: dict[int, list[MarketBar]],
    *,
    start: datetime,
    end: datetime,
    macro,
    levels: list[MarketLevel],
    calendar: NewsCalendar,
    openai_cache: dict[str, Any],
    research_cfg: OpenAIResearchConfig,
) -> list[dict[str, Any]]:
    cfg = IFVGAssistantConfig()
    warmup = max(cfg.atr_period + 5, 80)
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []

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
                openai_cache_path=OPENAI_CACHE,
                force_external_research=False,
            )
            if not setups:
                continue

            best = setups[0]
            d = setup_to_dict(best, timeframe_minutes=tf)
            external, openai_status = lookup_openai_cache(
                side=str(d.get("side") or ""),
                entry=best.plan.entry,
                zone_lo=best.candidate.gap_bot,
                zone_hi=best.candidate.gap_top,
                signal_ts=ts,
                cache=openai_cache,
            )
            grading = compute_setup_grading(best.score, external, research_config=research_cfg)
            letter = str(grading.get("letter") or "D")
            if letter not in {"A", "B", "C"}:
                continue

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
                    if len(sliced) >= 50:
                        bars_by_tf[t] = sliced

            side = c.side.value
            alignment = alignment_from_bars(bars_by_tf)
            macro_regime = macro_regime_for_side(side, macro, ts)
            dxy_delta = us10y_delta = None
            if macro is not None and macro.names():
                dxy_s = macro.get("dxy")
                us10y_s = macro.get("us10y")
                if dxy_s is not None:
                    dxy_delta = dxy_s.change(ts, cfg.macro_lookback_days)
                if us10y_s is not None:
                    us10y_delta = us10y_s.change(ts, cfg.macro_lookback_days)

            lvl_label, lvl_dist = _level_context(levels, best.plan.entry)
            atr = _atr(bars, i, cfg.atr_period)
            zone = zone_sl_plan(bars, c, atr, levels, cfg)
            structural = {
                "side": side,
                "entry": best.plan.entry,
                "sl": best.plan.stop,
                "tp1": best.plan.tp1,
                "tp2": best.plan.tp2,
                "tp3": best.plan.tp3,
                "risk_points": abs(best.plan.entry - best.plan.stop),
            }
            struct_2r = structural_2r_plan(structural)

            out.append({
                "signal_id": key,
                "timestamp": ts.isoformat(),
                "timeframe": tf,
                "side": side,
                "zone_low": round(c.gap_bot, 2),
                "zone_high": round(c.gap_top, 2),
                "bar_idx": i,
                "grade": letter,
                "checklist_pass": sum(1 for x in best.checklist if x.status == "pass"),
                "had_sweep": c.sweep_idx is not None,
                "htf_bias": compute_htf_bias_for_scout(bars_by_tf.get(240) or bars_by_tf.get(60) or bars),
                "alignment": alignment,
                "macro_regime": macro_regime,
                "dxy_bias": _instrument_bias(c.side, dxy_delta),
                "us10y_bias": _instrument_bias(c.side, us10y_delta),
                "news_risk": _news_context(calendar, ts),
                "nearest_market_level": lvl_label,
                "level_distance": lvl_dist,
                "session": session_of(bars[i]),
                "openai_status": openai_status,
                "zone_plan": zone,
                "structural_plan": structural,
                "structural_2r_plan": struct_2r,
            })
    return out


def build_trade_row(signal: dict[str, Any], model: str, sim: dict[str, Any], plan: dict[str, float]) -> dict[str, Any]:
    entry = plan["entry"]
    stop = plan["sl"]
    tp1 = plan["tp1"]
    risk_pts = abs(entry - stop)
    reward_tp1 = abs(tp1 - entry)
    rr_tp1 = round(reward_tp1 / risk_pts, 3) if risk_pts > 0 else 0.0
    lots = _size_lots(entry, stop)
    spread_usd, spread_r = _spread_r(entry, stop, lots)
    gross_r = sim.get("gross_r", 0.0)
    pnl_r = round(gross_r - spread_r, 4) if sim["result"] != "open" else 0.0
    pnl_usd = round(pnl_r * DEFAULT_RISK_USD, 2) if sim["result"] != "open" else 0.0

    return {
        "timestamp": signal["timestamp"],
        "timeframe": signal["timeframe"],
        "side": signal["side"],
        "zone_low": signal["zone_low"],
        "zone_high": signal["zone_high"],
        "entry": round(entry, 2),
        "sl": round(stop, 2),
        "tp1": round(tp1, 2),
        "tp2": round(plan.get("tp2", tp1), 2),
        "tp3": round(plan.get("tp3", tp1), 2),
        "model": model,
        "risk_points": round(risk_pts, 2),
        "reward_tp1_points": round(reward_tp1, 2),
        "rr_tp1": rr_tp1,
        "spread_cost": spread_usd,
        "result": sim["result"],
        "pnl_r": pnl_r,
        "max_favorable_excursion": sim.get("mfe", 0.0),
        "max_adverse_excursion": sim.get("mae", 0.0),
        "htf_bias": signal["htf_bias"],
        "alignment": signal["alignment"],
        "macro_regime": signal["macro_regime"],
        "dxy_bias": signal["dxy_bias"],
        "us10y_bias": signal["us10y_bias"],
        "news_risk": signal["news_risk"],
        "nearest_market_level": signal["nearest_market_level"],
        "level_distance": signal["level_distance"],
        "session": signal["session"],
        "grade": signal["grade"],
        "checklist_pass": signal["checklist_pass"],
        "had_sweep": signal["had_sweep"],
        "openai_status": signal["openai_status"],
        "pnl_usd_100acct": pnl_usd,
        "_exit_idx": sim.get("exit_idx"),
        "_signal_id": signal["signal_id"],
    }


def simulate_all_models(
    signals: list[dict[str, Any]],
    bars_by_tf: dict[int, list[MarketBar]],
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    for sig in signals:
        tf = int(sig["timeframe"])
        bars = bars_by_tf[tf]
        idx = sig["bar_idx"]
        horizon = {15: 384, 60: 168}.get(tf, 384)
        side = sig["side"]
        zone = sig["zone_plan"]
        structural = sig["structural_plan"]
        struct_2r = sig["structural_2r_plan"]

        sim_a = simulate_simple(
            bars, idx, side=side, entry=zone["entry"], stop=zone["sl"],
            target=zone["tp1"], max_bars=horizon,
        )
        trades.append(build_trade_row(sig, MODELS[0], sim_a, zone))

        sim_b = simulate_partial_runner(
            bars, idx, side=side, entry=zone["entry"], stop=zone["sl"],
            tp1=zone["tp1"], tp2=zone["tp2"], tp3=zone["tp3"], max_bars=horizon,
        )
        trades.append(build_trade_row(sig, MODELS[1], sim_b, zone))

        sim_c = simulate_simple(
            bars, idx, side=side, entry=structural["entry"], stop=structural["sl"],
            target=structural["tp1"], max_bars=horizon,
        )
        trades.append(build_trade_row(sig, MODELS[2], sim_c, structural))

        sim_d = simulate_simple(
            bars, idx, side=side, entry=struct_2r["entry"], stop=struct_2r["sl"],
            target=struct_2r["tp1"], max_bars=horizon,
        )
        trades.append(build_trade_row(sig, MODELS[3], sim_d, struct_2r))

    return [t for t in trades if t["result"] != "open"]


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"trades": 0, "win_rate": 0.0, "avg_r": 0.0, "total_r": 0.0, "profit_factor": 0.0, "max_dd_r": 0.0, "longest_losing_streak": 0}
    rs = [float(r["pnl_r"]) for r in rows]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    win_rate = len(wins) / len(rs)
    avg_r = stats.mean(rs)
    total_r = sum(rs)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss > 1e-9 else (999.0 if gross_win > 0 else 0.0)

    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    streak = 0
    max_streak = 0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if r <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    return {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "avg_r": round(avg_r, 4),
        "total_r": round(total_r, 2),
        "profit_factor": round(pf, 2),
        "max_dd_r": round(max_dd, 2),
        "longest_losing_streak": max_streak,
        "total_usd_100acct": round(sum(float(r.get("pnl_usd_100acct") or 0) for r in rows), 2),
    }


def _slice_metrics(trades: list[dict[str, Any]], key: str, min_trades: int = 5) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        val = str(t.get(key) or "unknown")
        buckets[val].append(t)
    out = {}
    for label, rows in sorted(buckets.items(), key=lambda kv: _metrics(kv[1])["total_r"], reverse=True):
        if len(rows) < min_trades:
            continue
        out[label] = _metrics(rows)
    return out


def sequential_one_position(trades: list[dict[str, Any]], bars_by_tf: dict[int, list[MarketBar]]) -> list[dict[str, Any]]:
    """One open trade per model at a time (same signal may run 4 models independently)."""
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        by_model[t["model"]].append(t)

    selected: list[dict[str, Any]] = []
    for model, rows in by_model.items():
        rows_sorted = sorted(rows, key=lambda r: r["timestamp"])
        busy_until: datetime | None = None
        for t in rows_sorted:
            ts = _parse_ts(t["timestamp"])
            if busy_until and ts <= busy_until:
                continue
            tf = int(t["timeframe"])
            bars = bars_by_tf[tf]
            exit_idx = t.get("_exit_idx")
            if exit_idx is not None and 0 <= exit_idx < len(bars):
                busy_until = bars[exit_idx].timestamp
            selected.append(t)
    return selected


def _rank_slices(slice_dict: dict[str, dict[str, Any]], n: int = 5) -> tuple[list[dict], list[dict]]:
    items = [{"slice": k, **v} for k, v in slice_dict.items()]
    best = sorted(items, key=lambda x: (x["total_r"], x["avg_r"]), reverse=True)[:n]
    worst = sorted(items, key=lambda x: (x["total_r"], x["avg_r"]))[:n]
    return best, worst


def write_markdown_report(report: dict[str, Any], path: Path) -> None:
    v = report["verdict"]
    mc = report["model_comparison"]["independent_per_signal"]
    ga = report.get("grade_a_only", {})
    lines = [
        "# IFVG Execution Geometry Audit",
        "",
        f"**Window:** {report['window']}",
        "",
        "## Hypothesis",
        report["hypothesis"],
        "",
        "## Entry assumption",
        report["methodology"]["entry"],
        "",
        "## Model comparison (independent per-signal, R-multiples, $30 risk ref)",
        "",
        "| Model | Trades | WR | Avg R | Total R | PF | Max DD (R) |",
        "|-------|--------|-----|-------|---------|-----|------------|",
    ]
    for model in MODELS:
        m = mc.get(model, {})
        wr = m.get("win_rate", 0) * 100
        lines.append(
            f"| {model} | {m.get('trades', 0)} | {wr:.1f}% | {m.get('avg_r', 0):+.3f} | "
            f"{m.get('total_r', 0):+.2f} | {m.get('profit_factor', 0):.2f} | {m.get('max_dd_r', 0):.2f} |"
        )

    lines.extend([
        "",
        "## Verdict",
        f"- **Best model:** {v['best_model']}",
        f"- **Zone SL vs structural:** {v['zone_vs_structural_verdict']}",
        f"- **Theory supported:** {'Yes' if v['theory_supported'] else 'No'} — {v['theory_note']}",
        "",
        "### Best sentiment slices (total R, min 5 trades)",
    ])
    for row in v.get("best_sentiment_slices", [])[:5]:
        lines.append(f"- `{row['dimension']}={row['slice']}`: {row['total_r']:+.2f} R ({row['trades']} tr, WR {row['win_rate']*100:.0f}%)")

    lines.extend(["", "### Grade A only"])
    if ga:
        lines.append(f"- Best model @ A: **{ga.get('best_model', '?')}** (total R {ga.get('best_total_r', 0):+.2f})")
        for model in MODELS:
            m = ga.get("models", {}).get(model, {})
            if m.get("trades"):
                lines.append(
                    f"  - {model}: {m['trades']} tr, WR {m.get('win_rate', 0)*100:.1f}%, "
                    f"avg R {m.get('avg_r', 0):+.3f}, total R {m.get('total_r', 0):+.2f}"
                )
    else:
        lines.append("- No grade A trades in window.")

    lines.extend(["", "## Outputs", f"- JSON: `{report['outputs']['json']}`", f"- CSV: `{report['outputs']['csv']}`"])
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    p = argparse.ArgumentParser(description="IFVG execution geometry audit")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--end", default="2026-05-29")
    p.add_argument("--data-dir", default=str(REPO / "data" / "agent_live_xauusd"))
    args = p.parse_args()

    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=23, minutes=59)
    start_dt = end_dt - timedelta(days=args.days)

    macro = load_macro_frame(REPO / "data" / "macro")
    if not macro.names():
        macro = None
    levels = load_market_levels(LEVELS)
    calendar = NewsCalendar.load(NEWS)
    openai_cache = _load_openai_cache()
    research_cfg = load_openai_research_config(OPENAI_CFG)
    research_cfg = OpenAIResearchConfig(enabled=False, mode="soft")

    data_dir = Path(args.data_dir)
    all_bars: dict[int, list[MarketBar]] = {}
    for tf, fname in ((5, "5m"), (15, "15m"), (60, "60m"), (240, "4h")):
        path = data_dir / f"xauusd_{fname}.csv"
        if not path.exists():
            continue
        full = load_bars_from_csv(path)
        all_bars[tf] = _filter_bars(full, start_dt - timedelta(days=7), end_dt)

    bars_by_tf = {tf: all_bars[tf] for tf in SCAN_TFS if tf in all_bars}

    print(f"Collecting IFVG signals M15/M60 ({start_dt.date()} → {end_dt.date()})...", flush=True)
    signals = collect_signals(
        all_bars,
        start=start_dt,
        end=end_dt,
        macro=macro,
        levels=levels,
        calendar=calendar,
        openai_cache=openai_cache,
        research_cfg=research_cfg,
    )
    print(f"  Unique signals (grade A/B/C): {len(signals)}", flush=True)

    print("Simulating 4 execution models per signal...", flush=True)
    trades = simulate_all_models(signals, bars_by_tf)
    print(f"  Closed trades logged: {len(trades)} ({len(signals)} signals × 4 models, minus open)", flush=True)

    independent_by_model = {m: _metrics([t for t in trades if t["model"] == m]) for m in MODELS}
    sequential_trades = sequential_one_position(trades, bars_by_tf)
    sequential_by_model = {m: _metrics([t for t in sequential_trades if t["model"] == m]) for m in MODELS}

    slice_dims = [
        "timeframe", "grade", "htf_bias", "alignment", "macro_regime",
        "dxy_bias", "us10y_bias", "session", "news_risk",
    ]
    slices: dict[str, dict[str, dict[str, Any]]] = {}
    best_model = max(MODELS, key=lambda m: independent_by_model[m]["total_r"])
    zone_models = ("IFVG_ZONE_SL_1R_2R", "IFVG_ZONE_SL_PARTIAL_RUNNER")
    struct_models = ("STRUCTURAL_SL_MICRO_TP_BASELINE", "STRUCTURAL_SL_2R")
    zone_total = sum(independent_by_model[m]["total_r"] for m in zone_models)
    struct_total = sum(independent_by_model[m]["total_r"] for m in struct_models)
    zone_avg = stats.mean([independent_by_model[m]["avg_r"] for m in zone_models])
    struct_avg = stats.mean([independent_by_model[m]["avg_r"] for m in struct_models])

    zone_wins = zone_total > struct_total and zone_avg > struct_avg
    theory_supported = zone_wins and independent_by_model["IFVG_ZONE_SL_1R_2R"]["avg_r"] >= 0

    for dim in slice_dims:
        slices[dim] = _slice_metrics([t for t in trades if t["model"] == best_model], dim)

    sentiment_slices: list[dict[str, Any]] = []
    for dim in ("dxy_bias", "us10y_bias", "htf_bias", "alignment", "macro_regime", "session", "news_risk"):
        for label, met in slices.get(dim, {}).items():
            sentiment_slices.append({"dimension": dim, "slice": label, **met})
    sentiment_slices.sort(key=lambda x: x["total_r"], reverse=True)
    best_sentiment, worst_sentiment = sentiment_slices[:5], list(reversed(sentiment_slices[-5:]))

    grade_a_trades = [t for t in trades if t["grade"] == "A"]
    grade_a_by_model = {m: _metrics([t for t in grade_a_trades if t["model"] == m]) for m in MODELS}
    grade_a_best = max(MODELS, key=lambda m: grade_a_by_model[m]["total_r"]) if grade_a_trades else None

    out_json = REPO / "logs" / "ifvg_execution_geometry_audit.json"
    out_csv = REPO / "logs" / "ifvg_execution_geometry_trades.csv"
    out_md = REPO / "docs" / "IFVG_EXECUTION_GEOMETRY_AUDIT.md"
    out_json.parent.mkdir(exist_ok=True)
    out_md.parent.mkdir(exist_ok=True)

    report = {
        "hypothesis": (
            "IFVG becomes profitable with zone-end SL (not wide structural sweep stop), "
            "min 1R TP1, partial/runner, sentiment as metadata then slice analysis."
        ),
        "window": f"{start_dt.date()} → {end_dt.date()} UTC ({args.days} days)",
        "methodology": {
            "signal_engine": "find_ifvg_setups (M15 + M60)",
            "grades": "A/B/C via compute_setup_grading; D excluded",
            "entry": "Signal-bar close (retest at close); no limit-in-zone, no hindsight",
            "models": list(MODELS),
            "primary_metric": "R-multiples (pnl_r); spread $0.70/0.01 lot RT; $30 risk reference",
            "portfolio_modes": {
                "independent_per_signal": "Each signal × model simulated independently (default rankings)",
                "sequential_one_position": "One open trade per model; skip overlapping entries",
            },
            "sentiment": "Tagged at entry; not filtered in main run",
            "data": str(data_dir),
        },
        "signal_count": len(signals),
        "model_comparison": {
            "independent_per_signal": independent_by_model,
            "sequential_one_position": sequential_by_model,
        },
        "zone_vs_structural": {
            "zone_models_total_r": round(zone_total, 2),
            "structural_models_total_r": round(struct_total, 2),
            "zone_models_avg_r": round(zone_avg, 4),
            "structural_models_avg_r": round(struct_avg, 4),
            "zone_wins_on_total_r": zone_total > struct_total,
        },
        "slices_by_best_model": {dim: slices[dim] for dim in slice_dims},
        "grade_a_only": {
            "best_model": grade_a_best,
            "best_total_r": grade_a_by_model[grade_a_best]["total_r"] if grade_a_best else 0,
            "models": grade_a_by_model,
        },
        "verdict": {
            "best_model": best_model,
            "zone_vs_structural_verdict": (
                "Zone SL preferred" if zone_wins else "Structural SL preferred (zone hypothesis not confirmed)"
            ),
            "theory_supported": theory_supported,
            "theory_note": (
                "Zone SL + min 1R TP1 shows positive edge vs structural baseline"
                if theory_supported
                else "Zone SL / 1R TP1 did not beat structural execution on aggregate R"
            ),
            "best_sentiment_slices": best_sentiment,
            "worst_sentiment_slices": worst_sentiment,
        },
        "outputs": {"json": str(out_json), "csv": str(out_csv), "markdown": str(out_md)},
    }

    out_json.write_text(json.dumps(report, indent=2))

    csv_rows = [{k: t[k] for k in TRADE_COLUMNS} for t in trades]
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
        w.writeheader()
        w.writerows(csv_rows)

    write_markdown_report(report, out_md)

    print("\n" + "=" * 72)
    print("IFVG EXECUTION GEOMETRY AUDIT")
    print("=" * 72)
    print(f"Signals: {len(signals)} | Closed trades: {len(trades)}")
    print(f"Best model (independent): {best_model}")
    print(f"Zone vs structural: {report['verdict']['zone_vs_structural_verdict']}")
    print(f"Theory supported: {theory_supported}")
    for model in MODELS:
        m = independent_by_model[model]
        print(
            f"  [{model}] tr={m['trades']} WR={m['win_rate']*100:.1f}% "
            f"avgR={m['avg_r']:+.3f} totalR={m['total_r']:+.2f} PF={m['profit_factor']:.2f}"
        )
    if grade_a_best:
        ga = grade_a_by_model[grade_a_best]
        print(f"\nGrade A best model: {grade_a_best} — {ga['trades']} tr, total R {ga['total_r']:+.2f}")
    print(f"\nReport: {out_json}")
    print(f"Trades: {out_csv}")
    print(f"Summary: {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
