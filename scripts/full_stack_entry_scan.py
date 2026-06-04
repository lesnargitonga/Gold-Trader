#!/usr/bin/env python3
"""Full-stack entry discovery — every replayable sentiment layer.

Walks all timeframe CSVs, replays ``build_bundle_snapshot`` with all 9 entry
families, and attaches bundle/macro/news/levels/IFVG/OpenAI/workflow context per
signal. Discovery only: no grade/R:R gates on collection.

Outputs:
  logs/full_stack_scan_signals.csv
  logs/full_stack_scan_report.json

Gaps (honest): live CME/options API, OpenAI web search per historical bar
(cache key uses hour bucket; no match → research_unavailable_at_signal).
Optional ``--with-openai-live`` calls API only for signals within the last 2h.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.assistants.ifvg_confluence import (  # noqa: E402
    IFVGAssistantConfig,
    load_market_levels,
)
from gold_trader.assistants.ifvg_workflow import build_workflow_context  # noqa: E402
from gold_trader.calendar import NewsCalendar  # noqa: E402
from gold_trader.data.csv_loader import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.models import Side  # noqa: E402
from gold_trader.research import analyze_timeframe_bundle, build_bundle_snapshot  # noqa: E402
from gold_trader.research.realtime_research import (  # noqa: E402
    OpenAIResearchConfig,
    RealtimeResearchResult,
    load_openai_research_config,
    run_openai_market_research,
)

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
OPENAI_CACHE = REPO / "data/cache/openai_market_research.json"
SHADOW = REPO / "logs" / "ifvg_shadow_journal.csv"

MAX_CANDIDATES_PER_SNAPSHOT = 500
MIN_BARS_PER_TF = 80
MACRO_LOOKBACK_DAYS = 5
LIVE_OPENAI_MAX_AGE_HOURS = 2

TF_ORDER = (5, 15, 60, 240)


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _slice_bars(bars, end: datetime):
    return [b for b in bars if b.timestamp <= end]


def _openai_cache_key(
    *,
    symbol: str,
    side: str,
    current_price: float,
    ifvg_zone_low: float,
    ifvg_zone_high: float,
    at: datetime,
) -> str:
    hour_bucket = at.strftime("%Y%m%d%H")
    norm_side = "buy" if side.lower() in {"long", "buy"} else "sell"
    return "|".join([
        symbol.upper(),
        norm_side,
        f"{round(float(current_price), 1):.1f}",
        f"{round(float(ifvg_zone_low), 1):.1f}",
        f"{round(float(ifvg_zone_high), 1):.1f}",
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


def lookup_openai_at_signal(
    *,
    symbol: str,
    side: str,
    entry: float,
    zone_lo: float,
    zone_hi: float,
    signal_ts: datetime,
    cache: dict[str, Any],
    allow_live: bool,
    research_cfg: OpenAIResearchConfig,
    checklist_rows: list[dict[str, Any]] | None,
    market_levels_rows: list[dict[str, Any]] | None,
    plan: dict[str, Any] | None,
) -> dict[str, Any]:
    """Historical cache lookup by signal-time hour bucket; optional live for recent signals."""
    keys_to_try = []
    for delta_h in (0, -1, 1, -2, 2):
        at = signal_ts + timedelta(hours=delta_h)
        keys_to_try.append(
            _openai_cache_key(
                symbol=symbol,
                side=side,
                current_price=entry,
                ifvg_zone_low=zone_lo,
                ifvg_zone_high=zone_hi,
                at=at,
            )
        )

    records = cache.get("records") or {}
    for key in keys_to_try:
        rec = records.get(key)
        if isinstance(rec, dict):
            result = _result_from_cache_record(rec)
            if result is not None:
                return _openai_row(result, status="cache_hit", cache_key=key)

    now = datetime.now(timezone.utc)
    age_h = (now - signal_ts).total_seconds() / 3600.0
    if allow_live and age_h <= LIVE_OPENAI_MAX_AGE_HOURS and research_cfg.enabled:
        p = plan or {}
        result = run_openai_market_research(
            symbol=symbol,
            side=side,
            current_price=entry,
            ifvg_zone_low=zone_lo,
            ifvg_zone_high=zone_hi,
            entry_low=float(p.get("entry_low") or zone_lo),
            entry_high=float(p.get("entry_high") or zone_hi),
            stop_loss=float(p.get("stop") or entry),
            tp1=float(p.get("tp1") or entry),
            tp2=float(p.get("tp2") or entry),
            tp3=float(p.get("tp3") or entry),
            technical_score=int(p.get("tech_score") or 65),
            checklist_rows=checklist_rows,
            market_levels=market_levels_rows,
            config_path=OPENAI_CFG,
            cache_path=OPENAI_CACHE,
            force_refresh=False,
        )
        return _openai_row(result, status="live_call", cache_key="")

    return _openai_unavailable()


def _openai_row(result: RealtimeResearchResult, *, status: str, cache_key: str) -> dict[str, Any]:
    opts = result.options or {}
    macro = result.macro or {}
    return {
        "openai_status": status,
        "openai_cache_key": cache_key,
        "openai_bias": result.bias,
        "openai_supports_trade": result.supports_trade,
        "openai_should_block": result.should_block_trade,
        "openai_confidence": result.confidence,
        "openai_news_risk": result.news_risk,
        "openai_dxy_bias": macro.get("dxy_bias", "unknown"),
        "openai_us10y_bias": macro.get("us10y_bias", "unknown"),
        "openai_real_yield_bias": macro.get("real_yield_bias", "unknown"),
        "openai_options_bias": opts.get("bias", "unknown"),
        "openai_options_levels": json.dumps(opts.get("important_levels") or []),
        "openai_danger_zones": json.dumps(opts.get("danger_zones") or []),
        "openai_summary": (result.summary or "")[:200],
        "options_proxy_source": "openai_cache_or_live",
        "cme_feed_available": False,
    }


def _openai_unavailable() -> dict[str, Any]:
    return {
        "openai_status": "research_unavailable_at_signal",
        "openai_cache_key": "",
        "openai_bias": "unknown",
        "openai_supports_trade": False,
        "openai_should_block": False,
        "openai_confidence": 0,
        "openai_news_risk": "unknown",
        "openai_dxy_bias": "unknown",
        "openai_us10y_bias": "unknown",
        "openai_real_yield_bias": "unknown",
        "openai_options_bias": "unknown",
        "openai_options_levels": "[]",
        "openai_danger_zones": "[]",
        "openai_summary": "",
        "options_proxy_source": "market_levels_json_only",
        "cme_feed_available": False,
    }


def _macro_context(macro, ts: datetime, side: Side) -> dict[str, Any]:
    out: dict[str, Any] = {
        "macro_dxy": "",
        "macro_us10y": "",
        "macro_real10y": "",
        "macro_dxy_chg_5d": "",
        "macro_us10y_chg_5d": "",
        "macro_real10y_chg_5d": "",
        "macro_regime": "unavailable",
    }
    if macro is None or not macro.names():
        return out

    deltas: dict[str, float] = {}
    for name in ("dxy", "us10y", "real10y"):
        series = macro.get(name)
        if series is None:
            continue
        level = series.as_of(ts)
        chg = series.change(ts, MACRO_LOOKBACK_DAYS)
        if level is not None:
            out[f"macro_{name}"] = round(level, 4)
        if chg is not None:
            out[f"macro_{name}_chg_5d"] = round(chg, 4)
            deltas[name] = chg

    if not deltas:
        out["macro_regime"] = "partial"
        return out

    good_long = sum(
        1
        for name, delta in deltas.items()
        if (side is Side.LONG and delta <= 0) or (side is Side.SHORT and delta >= 0)
    )
    if good_long >= 2:
        out["macro_regime"] = "aligned"
    elif good_long == 1:
        out["macro_regime"] = "mixed"
    else:
        out["macro_regime"] = "opposed"
    return out


def _level_context(
    levels,
    price: float,
    side: Side,
    atr: float,
    cfg: IFVGAssistantConfig,
) -> dict[str, Any]:
    if not levels:
        return {
            "nearest_level_label": "",
            "nearest_level_price": "",
            "nearest_level_dist": "",
            "level_danger_ahead": False,
            "level_danger_labels": "",
            "levels_source": str(LEVELS),
        }

    nearest = min(levels, key=lambda lv: abs(lv.price - price))
    dist = abs(nearest.price - price)
    danger = max(atr * cfg.level_danger_atr, 0.5) if atr > 0 else 0.5
    ahead: list[str] = []
    for level in levels:
        if side is Side.LONG and level.price > price and (level.price - price) <= danger:
            ahead.append(level.label or f"{level.kind}@{level.price:.0f}")
        if side is Side.SHORT and level.price < price and (price - level.price) <= danger:
            ahead.append(level.label or f"{level.kind}@{level.price:.0f}")

    return {
        "nearest_level_label": nearest.label or nearest.kind,
        "nearest_level_price": round(nearest.price, 2),
        "nearest_level_dist": round(dist, 2),
        "level_danger_ahead": bool(ahead),
        "level_danger_labels": ";".join(ahead[:3]),
        "levels_source": str(LEVELS),
    }


def _tf_columns(analysis, snap) -> dict[str, Any]:
    profiles = {p.timeframe_minutes: p for p in analysis.profiles}
    states = {s.timeframe_minutes: s for s in snap.timeframe_states}
    cols: dict[str, Any] = {}
    for tf in TF_ORDER:
        prefix = f"tf_{tf}"
        prof = profiles.get(tf)
        st = states.get(tf)
        if prof is None and st is None:
            cols[f"{prefix}_trend"] = ""
            cols[f"{prefix}_structure"] = ""
            cols[f"{prefix}_rsi"] = ""
            cols[f"{prefix}_macd"] = ""
            cols[f"{prefix}_trend_strength"] = ""
            continue
        cols[f"{prefix}_trend"] = (st.trend_state if st else prof.trend_state) if (st or prof) else ""
        cols[f"{prefix}_structure"] = st.structure_state if st else ""
        cols[f"{prefix}_rsi"] = round(st.rsi14, 2) if st else round(prof.rsi14, 2)
        cols[f"{prefix}_macd"] = round(prof.macd, 4) if prof else ""
        cols[f"{prefix}_trend_strength"] = round(st.trend_strength if st else prof.trend_strength, 3)
    return cols


def _ifvg_checklist_cols(details: dict[str, Any] | None) -> dict[str, Any]:
    if not details:
        return {
            "ifvg_grade": "",
            "ifvg_tech_score": "",
            "ifvg_verdict": "",
            "ifvg_checklist_pass": "",
            "ifvg_checklist_total": "",
            "ifvg_checklist_fail_names": "",
        }
    checklist = details.get("checklist") or []
    fails = [str(c.get("name")) for c in checklist if c.get("status") == "fail"]
    grading = details.get("grading") or {}
    return {
        "ifvg_grade": str(grading.get("letter") or details.get("grade") or "?"),
        "ifvg_tech_score": int(details.get("score") or 0),
        "ifvg_verdict": str(details.get("verdict") or ""),
        "ifvg_checklist_pass": sum(1 for c in checklist if c.get("status") == "pass"),
        "ifvg_checklist_total": len(checklist),
        "ifvg_checklist_fail_names": ",".join(fails),
    }


def _workflow_cols(
    details: dict[str, Any] | None,
    *,
    datasets: dict[int, list],
    entry: float,
    market_levels,
) -> dict[str, Any]:
    if not details:
        return {
            "workflow_passes": "",
            "workflow_total_steps": "",
            "workflow_ready": "",
            "workflow_blockers": "",
            "workflow_entry_type": "",
        }
    tf = int(details.get("timeframe_minutes") or 15)
    primary = datasets.get(tf) or []
    wf = build_workflow_context(
        details,
        primary_bars=primary,
        current_price=entry,
        bars_by_tf=datasets,
        market_levels=market_levels,
    )
    return {
        "workflow_passes": wf.get("passes"),
        "workflow_total_steps": wf.get("total_steps"),
        "workflow_ready": wf.get("workflow_ready"),
        "workflow_blockers": " | ".join(wf.get("blockers") or [])[:300],
        "workflow_entry_type": wf.get("entry_type") or "",
    }


def collect_signals(
    all_bars: dict[int, list],
    *,
    start: datetime,
    end: datetime,
    macro,
    cadence_minutes: int,
    allow_openai_live: bool,
) -> list[dict]:
    anchor_bars = all_bars.get(cadence_minutes) or all_bars.get(15) or []
    step_bars = [b for b in anchor_bars if start <= b.timestamp <= end]
    if not step_bars:
        return []

    calendar = NewsCalendar.load(NEWS)
    levels = load_market_levels(LEVELS)
    level_cfg = IFVGAssistantConfig()
    research_cfg = load_openai_research_config(OPENAI_CFG)
    if not allow_openai_live:
        research_cfg = OpenAIResearchConfig(enabled=False, mode="off")

    openai_cache = _load_openai_cache()
    levels_json = [
        {"price": lv.price, "kind": lv.kind, "label": lv.label, "strength": lv.strength}
        for lv in levels
    ]

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

        analysis = analyze_timeframe_bundle(datasets)
        snap = build_bundle_snapshot(
            datasets,
            families=FULL_FAMILIES,
            max_candidates=MAX_CANDIDATES_PER_SNAPSHOT,
            macro_frame=macro,
            market_levels_path=str(LEVELS),
            news_calendar_path=str(NEWS),
            shadow_journal_path=str(SHADOW),
            openai_research_config_path=str(OPENAI_CFG),
            openai_research_cache_path=str(OPENAI_CACHE),
        )

        top = snap.entry_candidates[0] if snap.entry_candidates else None
        decision = snap.decision
        decision_rationale = " | ".join(decision.rationale) if decision.rationale else ""
        tf_cols = _tf_columns(analysis, snap)
        profiles = {p.timeframe_minutes: p for p in analysis.profiles}

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
            prof = profiles.get(cand.timeframe_minutes)
            atr = prof.atr14 if prof else 0.0

            blocked, ev = calendar.is_blackout(bar_ts, window_minutes=30)
            details = cand.details or {}

            row: dict[str, Any] = {
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
                "news_blackout": blocked,
                "news_event": ev.event if ev else "",
                **tf_cols,
                **_macro_context(macro, bar_ts, cand.side),
                **_level_context(levels, cand.reference_price, cand.side, atr, level_cfg),
            }

            if cand.family == "inversion_fair_value_gap":
                row.update(_ifvg_checklist_cols(details))
                zone = details.get("zone") or {}
                plan = details.get("entry_plan") or {}
                row.update(
                    lookup_openai_at_signal(
                        symbol="XAUUSD",
                        side=cand.side.value,
                        entry=cand.reference_price,
                        zone_lo=float(zone.get("bot") or cand.reference_price),
                        zone_hi=float(zone.get("top") or cand.reference_price),
                        signal_ts=bar_ts,
                        cache=openai_cache,
                        allow_live=allow_openai_live,
                        research_cfg=research_cfg,
                        checklist_rows=list(details.get("checklist") or []),
                        market_levels_rows=levels_json,
                        plan={**plan, "tech_score": details.get("score")},
                    )
                )
                row.update(
                    _workflow_cols(
                        details,
                        datasets=datasets,
                        entry=cand.reference_price,
                        market_levels=levels,
                    )
                )
            else:
                row.update(_ifvg_checklist_cols(None))
                row.update(_openai_unavailable())
                row.update(_workflow_cols(None, datasets=datasets, entry=cand.reference_price, market_levels=levels))

            signals.append(row)

    return signals


SIGNAL_FIELDS: tuple[str, ...] = (
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
    "ifvg_checklist_pass",
    "ifvg_checklist_total",
    "ifvg_checklist_fail_names",
    "htf_bias",
    "alignment",
    "oscillation",
    "decision_status",
    "decision_family",
    "decision_is_top",
    "decision_rationale",
    "warnings",
    "news_blackout",
    "news_event",
    "macro_dxy",
    "macro_us10y",
    "macro_real10y",
    "macro_dxy_chg_5d",
    "macro_us10y_chg_5d",
    "macro_real10y_chg_5d",
    "macro_regime",
    "nearest_level_label",
    "nearest_level_price",
    "nearest_level_dist",
    "level_danger_ahead",
    "level_danger_labels",
    "levels_source",
    "openai_status",
    "openai_cache_key",
    "openai_bias",
    "openai_supports_trade",
    "openai_should_block",
    "openai_confidence",
    "openai_news_risk",
    "openai_dxy_bias",
    "openai_us10y_bias",
    "openai_real_yield_bias",
    "openai_options_bias",
    "openai_options_levels",
    "openai_danger_zones",
    "openai_summary",
    "options_proxy_source",
    "cme_feed_available",
    "workflow_passes",
    "workflow_total_steps",
    "workflow_ready",
    "workflow_blockers",
    "workflow_entry_type",
    "tf_5_trend",
    "tf_5_structure",
    "tf_5_rsi",
    "tf_5_macd",
    "tf_5_trend_strength",
    "tf_15_trend",
    "tf_15_structure",
    "tf_15_rsi",
    "tf_15_macd",
    "tf_15_trend_strength",
    "tf_60_trend",
    "tf_60_structure",
    "tf_60_rsi",
    "tf_60_macd",
    "tf_60_trend_strength",
    "tf_240_trend",
    "tf_240_structure",
    "tf_240_rsi",
    "tf_240_macd",
    "tf_240_trend_strength",
)


def main() -> int:
    p = argparse.ArgumentParser(description="Full-stack entry scan (all replayable layers)")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--start", default="2026-04-29", help="Window start (overrides --days if set with --end)")
    p.add_argument("--end", default="2026-05-29")
    p.add_argument("--data-dir", default=str(REPO / "data" / "agent_live_xauusd"))
    p.add_argument("--cadence", type=int, default=60)
    p.add_argument(
        "--with-openai-live",
        action="store_true",
        help="Call OpenAI for signals within last 2h only (forward); historical uses cache lookup",
    )
    args = p.parse_args()

    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=23, minutes=59)
    if args.start:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
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
        allow_openai_live=args.with_openai_live,
    )
    print(f"Unique entry candidates: {len(signals)}", flush=True)

    from collections import Counter

    report = {
        "methodology": {
            "purpose": "full_stack_entry_discovery",
            "engine": "gold_trader.research.state.build_bundle_snapshot",
            "families": list(FULL_FAMILIES),
            "layers_wired": {
                "entry_families": "all 9 from state.py",
                "timeframes": loaded_tfs,
                "bundle_sentiment": "htf_bias, alignment, oscillation, per-TF trend/rsi/macd/structure",
                "macro_csv": "dxy, us10y, real10y levels + 5d deltas + macro_regime",
                "news_calendar": str(NEWS),
                "market_levels": str(LEVELS),
                "ifvg_checklist_grades": "A/B/C/D from setup details",
                "openai": "cache lookup by signal hour bucket; else research_unavailable_at_signal",
                "options_cme_proxy": "market_levels.json + openai cache options block; no CME API",
                "ifvg_workflow": "8-step build_workflow_context metadata",
                "bundle_decision": "accept/reject/hold metadata only",
            },
            "gaps": {
                "cme_live_feed": False,
                "openai_historical_web": "No per-bar API replay; cache hour-bucket match only",
                "openai_cache_key": "Uses signal-time hour bucket (live code uses now())",
                "m1_bars": "Workflow step 4 M1 confirmation needs M1 CSV if absent",
            },
            "window": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "cadence_minutes": args.cadence,
        },
        "discovery": {
            "total": len(signals),
            "by_family": dict(Counter(s["family"] for s in signals)),
            "by_openai_status": dict(Counter(s.get("openai_status") for s in signals if s["family"] == "inversion_fair_value_gap")),
        },
    }

    logs = REPO / "logs"
    logs.mkdir(exist_ok=True)
    csv_path = logs / "full_stack_scan_signals.csv"
    json_path = logs / "full_stack_scan_report.json"

    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(SIGNAL_FIELDS), extrasaction="ignore")
        w.writeheader()
        w.writerows(signals)

    json_path.write_text(json.dumps(report, indent=2))
    print(f"Wrote {csv_path} ({len(signals)} rows, {len(SIGNAL_FIELDS)} columns)")
    print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
