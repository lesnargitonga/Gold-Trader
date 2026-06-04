"""Automatic IFVG scout — background AI watch loop + operator approval brief.

Runs on an interval (``scripts/ifvg_auto_scout.py``), writes
``logs/ifvg_scout_state.json``, and feeds the Trade UI. Model-facing alerts
accumulate in state so each scan knows what changed and what to watch next.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..calendar import NewsCalendar
from ..data.macro import load_macro_frame
from ..infra.secrets import resolve_openai_api_key
from ..live.broker import BrokerError
from ..live.mt5_bridge_client import MT5RemoteBroker
from ..models import MarketBar, Side
from .ifvg_confluence import find_ifvg_setups, load_market_levels, setup_to_dict
from .ifvg_workflow import (
    WORKFLOW_FORMULA,
    alignment_from_bars,
    build_workflow_context,
    compute_htf_bias_for_scout,
    evaluate_live_sentiment,
    macro_regime_for_side,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STATE_PATH = REPO_ROOT / "logs" / "ifvg_scout_state.json"
DEFAULT_TF_PREF_PATH = REPO_ROOT / "logs" / "ifvg_scout_tf.json"
DEFAULT_LOG_PATH = REPO_ROOT / "logs" / "ifvg_scout.log"
DATA_DIR = REPO_ROOT / "data"
OPENAI_CFG = REPO_ROOT / "config" / "openai_research.json"
RESEARCH_CACHE = DATA_DIR / "cache" / "openai_market_research.json"
LEVELS_CFG = REPO_ROOT / "config" / "market_levels.json"
NEWS_CAL = DATA_DIR / "macro" / "news_calendar.csv"

DEFAULT_SCAN_SECONDS = 60
DEFAULT_PRIMARY_TF = 15
WATCH_TFS = (15, 60)
LIVE_RISK_USD = 30.0
LIVE_GRADE_AUDIT_NOTE = "30-day audit (Apr–May 2026): only grade A is live-eligible"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utcnow()).isoformat()


def read_scout_timeframe(path: Path | None = None) -> int:
    """Primary chart TF for the background scout (UI writes this file)."""
    env_tf = os.environ.get("IFVG_SCOUT_TF")
    if env_tf:
        try:
            return int(env_tf)
        except ValueError:
            pass
    p = path or DEFAULT_TF_PREF_PATH
    if p.exists():
        try:
            data = json.loads(p.read_text())
            if isinstance(data, dict) and data.get("timeframe") is not None:
                return int(data["timeframe"])
        except Exception:
            pass
    return DEFAULT_PRIMARY_TF


def write_scout_timeframe(timeframe: int, path: Path | None = None) -> None:
    p = path or DEFAULT_TF_PREF_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"timeframe": int(timeframe), "updated_at": _iso()}, indent=2))


def load_scout_state(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_STATE_PATH
    if not p.exists():
        return _empty_state()
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else _empty_state()
    except Exception:
        return _empty_state()


def save_scout_state(state: dict[str, Any], path: Path | None = None) -> None:
    p = path or DEFAULT_STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(p)


def _empty_state() -> dict[str, Any]:
    now = _iso()
    return {
        "status": "starting",
        "last_scan_at": None,
        "next_scan_at": None,
        "scan_interval_sec": DEFAULT_SCAN_SECONDS,
        "primary_timeframe": DEFAULT_PRIMARY_TF,
        "bridge_online": False,
        "data_source": "unknown",
        "setup": None,
        "approval_brief": None,
        "model_alerts": [],
        "scan_count": 0,
        "started_at": now,
        "openai_configured": bool(resolve_openai_api_key()),
    }


def scout_log(message: str, *, level: str = "INFO", path: Path | None = None) -> None:
    p = path or DEFAULT_LOG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{_iso()}] {level:5s} {message}\n"
    with p.open("a") as handle:
        handle.write(line)


def append_model_alert(
    state: dict[str, Any],
    *,
    kind: str,
    message: str,
    watch_for: str = "",
    price_zone: list[float] | None = None,
    timeframe: int | None = None,
) -> None:
    alerts: list[dict[str, Any]] = list(state.get("model_alerts") or [])
    entry = {
        "ts": _iso(),
        "kind": kind,
        "message": message,
        "watch_for": watch_for,
        "timeframe": timeframe,
        "price_zone": price_zone,
    }
    # De-dupe consecutive identical messages
    if alerts and alerts[-1].get("message") == message and alerts[-1].get("kind") == kind:
        return
    alerts.append(entry)
    state["model_alerts"] = alerts[-30:]


def build_approval_brief(
    setup: dict[str, Any] | None,
    workflow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workflow = workflow or (setup or {}).get("workflow") or {}
    wf_steps = list(workflow.get("steps") or [])

    if not setup:
        htf = workflow.get("htf_bias") or {}
        blockers = list(workflow.get("blockers") or [])
        if not blockers:
            blockers = ["No active IFVG setup on the selected timeframe"]
        return {
            "can_enter": False,
            "headline": "No tradable setup — AI is watching",
            "summary": f"HTF {htf.get('combined', 'scanning')} · {WORKFLOW_FORMULA}",
            "formula": workflow.get("formula") or WORKFLOW_FORMULA,
            "workflow_steps": wf_steps,
            "entry_type": workflow.get("entry_type"),
            "reasons": [],
            "blockers": blockers,
            "model_watch": ["Scanning for sweep → displacement → inversion → retest"],
        }

    verdict = str(setup.get("verdict") or "")
    grading = setup.get("grading") or {}
    research_mode = str((setup.get("external_research") or {}).get("mode") or grading.get("research_mode") or "soft")
    blocked = bool(setup.get("externally_blocked")) and research_mode == "hard"
    final_letter = str(grading.get("letter") or setup.get("grade") or "D")
    tech_score = int(setup.get("score") or 0)
    sentiment = workflow.get("live_sentiment") or {}
    sentiment_blockers = list(sentiment.get("blockers") or [])
    sentiment_warnings = list(sentiment.get("warnings") or [])
    workflow_ready = bool(workflow.get("workflow_ready"))
    can_enter = (
        not blocked
        and final_letter == "A"
        and verdict in ("valid_entry", "alert_wait")
        and tech_score >= 65
        and workflow_ready
        and not sentiment_blockers
    )
    wf_hard_fails = [s for s in wf_steps if s.get("status") == "fail" and int(s.get("step") or 0) in (1, 3)]
    if wf_hard_fails:
        can_enter = False
    plan = setup.get("entry_plan") or {}
    zone = setup.get("zone") or {}
    ext = setup.get("external_research") or {}
    macro = ext.get("macro") or {}
    opts = ext.get("options") or {}

    reasons: list[dict[str, str]] = []
    for item in setup.get("checklist") or []:
        st = item.get("status") or "fail"
        if st == "fail" and item.get("points", 0) == 0:
            continue
        reasons.append({
            "title": str(item.get("label") or item.get("name") or "Check"),
            "detail": str(item.get("reason") or f"{item.get('points')}/{item.get('max_points')}"),
            "status": "pass" if st == "pass" else ("warn" if st == "partial" else "fail"),
        })

    if setup.get("score") is not None:
        reasons.insert(0, {
            "title": f"Final grade {final_letter}",
            "detail": (
                f"Technical IFVG {tech_score}/100 ({grading.get('technical_letter', '?')}) · "
                f"External {grading.get('external_confirmation', 'off')} · "
                f"{grading.get('action', '')}"
            ),
            "status": "pass" if final_letter == "A" else ("warn" if final_letter in ("B", "C") else "fail"),
        })

    if ext.get("enabled"):
        ext_mode = ext.get("mode") or research_mode
        status = "pass" if ext.get("supports_trade") and not ext.get("should_block_trade") else "warn"
        if ext.get("should_block_trade") or blocked:
            status = "fail" if ext_mode == "hard" else "warn"
        reasons.append({
            "title": "External research (grading only in soft mode)",
            "detail": str(ext.get("summary") or setup.get("ai_verdict") or "Research complete"),
            "status": status,
        })
        if macro.get("dxy_bias") and macro.get("dxy_bias") != "unknown":
            reasons.append({
                "title": "Macro — DXY / yields",
                "detail": f"DXY {macro.get('dxy_bias')} · US10Y {macro.get('us10y_bias', '?')}",
                "status": "pass" if "supports" in str(macro.get("dxy_bias", "")) else "neutral",
            })
        if opts.get("bias") and opts.get("bias") != "unknown":
            lv = opts.get("important_levels") or []
            reasons.append({
                "title": "Options / flow",
                "detail": f"Bias {opts.get('bias')}" + (f" · strikes {', '.join(str(x) for x in lv[:4])}" if lv else ""),
                "status": "pass" if "bullish" in str(opts.get("bias")) and setup.get("side") == "long"
                else ("pass" if "bearish" in str(opts.get("bias")) and setup.get("side") == "short" else "neutral"),
            })

    blockers: list[str] = []
    model_watch_pre: str | None = None
    for s in wf_hard_fails:
        blockers.append(str(s.get("detail") or s.get("title") or "Workflow gate failed"))
    blockers.extend(sentiment_blockers)
    if not can_enter:
        if final_letter in ("B", "C"):
            blockers.append(f"Grade {final_letter} blocked for live — {LIVE_GRADE_AUDIT_NOTE}")
        elif final_letter == "D":
            blockers.append(str(grading.get("action") or f"Grade {final_letter} — watch or avoid"))
        elif not workflow_ready:
            blockers.append("Workflow not ready — resolve workflow blockers before live entry")
        elif verdict == "externally_blocked":
            blockers.append("Externally blocked (hard mode only)")
        elif verdict == "ignore":
            blockers.append("No tradable IFVG on this timeframe")
        elif tech_score < 65:
            blockers.append(f"IFVG score {tech_score} below minimum (65)")
        elif final_letter != "A":
            blockers.append(f"Grade {final_letter} — live profile requires grade A")
        elif verdict not in ("valid_entry", "alert_wait"):
            blockers.append(f"Verdict «{verdict}»")
        elif sentiment_blockers:
            pass
    if blocked:
        blockers.append("External research blocks this direction (hard mode only)")
    for w in setup.get("warnings") or []:
        if str(w).startswith("external_research:") and research_mode != "hard":
            continue
        blockers.append(str(w))
    if ext.get("should_block_trade") and research_mode == "hard":
        blockers.append(str(ext.get("summary") or "Research recommends waiting"))
    elif ext.get("should_block_trade"):
        model_watch_pre = str(ext.get("summary") or "External context mixed — grade reduced")
    else:
        model_watch_pre = None
    if ext.get("news_risk") == "high" and ext.get("enabled"):
        if research_mode == "hard":
            blockers.append("High news risk window")
        else:
            model_watch_pre = (model_watch_pre or "") + " · High news risk — wait until after release"

    model_watch: list[str] = []
    if model_watch_pre:
        model_watch.append(model_watch_pre.strip(" ·"))
    for w in sentiment_warnings:
        model_watch.append(str(w))
    for w in grading.get("external_warnings") or []:
        model_watch.append(str(w))
    side = str(setup.get("side") or "").upper()
    inversion_note = str(setup.get("inversion_note") or "").strip()
    if inversion_note:
        model_watch.append(inversion_note)
    zlo, zhi = zone.get("bot"), zone.get("top")
    if zlo is not None and zhi is not None:
        action = "BUY" if side == "LONG" else ("SELL" if side == "SHORT" else side)
        model_watch.append(
            f"{action} IFVG zone {float(zlo):.2f}–{float(zhi):.2f} — watch retest reaction (reject vs break)"
        )
    if plan.get("stop") is not None:
        model_watch.append(f"Hard stop {float(plan['stop']):.2f} · invalidation {plan.get('invalidation', plan.get('stop'))}")
    if plan.get("tp1") is not None:
        tp_parts = [f"TP1 {float(plan['tp1']):.2f} (min 1R floor)"]
        if plan.get("tp2") is not None:
            tp_parts.append(f"TP2 {float(plan['tp2']):.2f}")
        if plan.get("tp3") is not None:
            tp_parts.append(f"TP3 {float(plan['tp3']):.2f}")
        tp_parts.append("targets from swings + market_levels — no fixed 2R runner cap")
        model_watch.append(" · ".join(tp_parts))
    for lv in (opts.get("important_levels") or [])[:4]:
        model_watch.append(f"Options strike magnet near {float(lv):.2f}")
    for dz in (opts.get("danger_zones") or [])[:2]:
        model_watch.append(f"Danger zone {float(dz):.2f} — avoid chasing into liquidity")

    style = str(setup.get("trade_style") or "trade")
    entry_type = workflow.get("entry_type") or "pullback"
    if can_enter:
        headline = f"Grade A ready — manual Enter trade · {side} {style} ({entry_type})"
        summary = (
            str(setup.get("ai_verdict") or grading.get("action") or "IFVG grade A passes live profile.")
            + " Manual approval required — bundle accept/reject is not permission to trade."
        )
    elif final_letter in ("B", "C"):
        headline = f"Watch — grade {final_letter} · paper only (live blocked)"
        summary = f"{LIVE_GRADE_AUDIT_NOTE}. {grading.get('action') or setup.get('ai_verdict') or 'Reduce confidence; not live-eligible.'}"
    elif final_letter == "D":
        headline = f"Watch — grade {final_letter} · lower quality setup"
        summary = str(grading.get("action") or setup.get("ai_verdict") or "Reduce confidence; not live-eligible.")
    elif verdict == "alert_wait":
        headline = f"Wait — {side} setup forming"
        summary = str(setup.get("ai_verdict") or "Almost ready — AI will promote when checklist completes.")
    else:
        headline = "Watching — no entry yet"
        summary = str(setup.get("ai_verdict") or "Scanning live bars for the next IFVG opportunity.")

    return {
        "can_enter": can_enter,
        "headline": headline,
        "summary": summary,
        "formula": workflow.get("formula") or WORKFLOW_FORMULA,
        "workflow_steps": wf_steps,
        "entry_type": entry_type,
        "entry_type_detail": workflow.get("entry_type_detail"),
        "htf_bias": workflow.get("htf_bias"),
        "price_position": workflow.get("price_position"),
        "workflow_passes": workflow.get("passes"),
        "grading": grading,
        "final_grade": final_letter,
        "suggested_risk_pct": grading.get("suggested_risk_pct"),
        "suggested_risk_usd": LIVE_RISK_USD if final_letter == "A" else None,
        "manual_approval_required": True,
        "live_profile": "grade_a_only",
        "research_mode": research_mode,
        "live_sentiment": sentiment or None,
        "reasons": reasons,
        "blockers": blockers if not can_enter else [],
        "model_watch": model_watch,
        "trade": {
            "side": setup.get("side"),
            "style": setup.get("trade_style"),
            "entry_low": plan.get("entry_low"),
            "entry_high": plan.get("entry_high"),
            "stop": plan.get("stop"),
            "tp1": plan.get("tp1"),
            "tp2": plan.get("tp2"),
            "tp3": plan.get("tp3"),
        },
    }


def _bridge_client() -> MT5RemoteBroker:
    url = os.environ.get("GOLD_BRIDGE_URL", "http://127.0.0.1:8765")
    secret = os.environ.get("GOLD_BRIDGE_SECRET", "")
    return MT5RemoteBroker(base_url=url, shared_secret=secret, timeout=8.0)


def _load_bars(timeframe: int, count: int = 900) -> tuple[list[MarketBar], str, bool]:
    symbol = os.environ.get("GOLD_SYMBOL", "GOLD")
    try:
        client = _bridge_client()
        client.healthz()
        rows = client.get_candles(symbol=symbol, timeframe_minutes=timeframe, count=count)
        bars: list[MarketBar] = []
        for row in rows or []:
            try:
                ts = datetime.fromtimestamp(float(row["time"]), timezone.utc)
                bars.append(MarketBar(
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("tick_volume") or row.get("volume") or 0),
                    spread=float(row.get("spread") or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        if bars:
            return bars, "bridge", True
    except (BrokerError, OSError, ValueError):
        pass

    suffix = f"_{timeframe}m.csv"
    matches: list[tuple[float, Path]] = []
    for p in DATA_DIR.rglob(f"*{suffix}"):
        if p.is_file():
            matches.append((p.stat().st_mtime, p))
    if not matches:
        return [], "none", False
    matches.sort(reverse=True)
    path = matches[0][1]
    from ..data.csv_loader import load_bars_from_csv
    return list(load_bars_from_csv(path))[-count:], "csv_fallback", False


def _price_near_zone(price: float, lo: float, hi: float, atr: float) -> bool:
    pad = max(atr * 0.35, 2.0)
    return (lo - pad) <= price <= (hi + pad)


def run_scout_scan(
    *,
    primary_tf: int = DEFAULT_PRIMARY_TF,
    force_research: bool = False,
    state_path: Path | None = None,
    log_path: Path | None = None,
) -> dict[str, Any]:
    state = load_scout_state(state_path)
    now = _utcnow()
    interval = int(state.get("scan_interval_sec") or DEFAULT_SCAN_SECONDS)

    bars, source, bridge_online = _load_bars(primary_tf)
    state["bridge_online"] = bridge_online
    state["data_source"] = source
    state["primary_timeframe"] = primary_tf
    state["last_scan_at"] = _iso(now)
    from datetime import timedelta
    state["next_scan_at"] = (now + timedelta(seconds=interval)).isoformat()
    state["scan_count"] = int(state.get("scan_count") or 0) + 1
    state["openai_configured"] = bool(resolve_openai_api_key())
    levels = load_market_levels(LEVELS_CFG)

    if not bars:
        state["status"] = "offline"
        state["setup"] = None
        state["workflow"] = build_workflow_context(None, primary_bars=[], market_levels=levels)
        state["approval_brief"] = build_approval_brief(None, state["workflow"])
        append_model_alert(
            state,
            kind="data",
            message="No live bars — waiting for MT5 bridge or CSV fallback",
            watch_for="Restore bridge; run ./start",
            timeframe=primary_tf,
        )
        save_scout_state(state, state_path)
        scout_log("scan skipped — no bars", level="WARN", path=log_path)
        return state

    macro_frame = load_macro_frame(DATA_DIR / "macro")
    if not macro_frame.names():
        macro_frame = None
    calendar = NewsCalendar.load(NEWS_CAL)

    prev_setup = state.get("setup") or {}
    prev_verdict = prev_setup.get("verdict")
    last_price = bars[-1].close

    bars_by_tf: dict[int, list[MarketBar]] = {primary_tf: list(bars)}
    for ltf in (5, 1):
        if ltf == primary_tf:
            continue
        lb, _, _ = _load_bars(ltf, count=240 if ltf == 5 else 120)
        if lb:
            bars_by_tf[ltf] = lb

    htf_bias = compute_htf_bias_for_scout(bars)
    scout_alerts = list(state.get("model_alerts") or [])[-8:]
    setups = find_ifvg_setups(
        bars,
        macro_frame=macro_frame,
        market_levels=levels,
        news_calendar=calendar,
        higher_timeframe_bias=htf_bias if htf_bias != "neutral" else None,
        openai_config_path=OPENAI_CFG,
        openai_cache_path=RESEARCH_CACHE,
        force_external_research=force_research,
        scout_alerts=scout_alerts,
    )
    best = setups[0] if setups else None
    setup_dict = setup_to_dict(best, timeframe_minutes=primary_tf) if best else None
    workflow = build_workflow_context(
        setup_dict,
        primary_bars=bars,
        current_price=last_price,
        bars_by_tf=bars_by_tf,
        market_levels=levels,
    )
    for tf in (*WATCH_TFS, 240):
        if tf != primary_tf and tf not in bars_by_tf:
            lb, _, _ = _load_bars(tf, count=400)
            if lb:
                bars_by_tf[tf] = lb
    if setup_dict is not None:
        side = str(setup_dict.get("side") or "")
        workflow["live_sentiment"] = evaluate_live_sentiment(
            side=side,
            alignment=alignment_from_bars(bars_by_tf),
            macro_regime=macro_regime_for_side(
                side,
                macro_frame,
                bars[-1].timestamp,
            ),
        )
        setup_dict["workflow"] = workflow
    state["workflow"] = workflow
    state["setup"] = setup_dict
    state["approval_brief"] = build_approval_brief(setup_dict, workflow)
    brief = state["approval_brief"]
    if setup_dict:
        zone = setup_dict.get("zone") or {}
        zlo, zhi = float(zone.get("bot", 0)), float(zone.get("top", 0))
        lo, hi = min(zlo, zhi), max(zlo, zhi)
        atr = _simple_atr(bars)
        if _price_near_zone(last_price, lo, hi, atr):
            append_model_alert(
                state,
                kind="proximity",
                message=f"Price {last_price:.2f} testing IFVG {lo:.2f}–{hi:.2f} on M{primary_tf}",
                watch_for="Reject vs break close — retest quality decides entry",
                price_zone=[lo, hi],
                timeframe=primary_tf,
            )
        new_verdict = setup_dict.get("verdict")
        if new_verdict == "valid_entry" and prev_verdict != "valid_entry":
            append_model_alert(
                state,
                kind="entry_ready",
                message=f"VALID ENTRY — {setup_dict.get('side', '').upper()} M{primary_tf} score {setup_dict.get('score')}",
                watch_for="Manual approval only — review brief blockers before Enter trade",
                price_zone=[lo, hi],
                timeframe=primary_tf,
            )
            scout_log(f"ENTRY READY {setup_dict.get('side')} M{primary_tf} score={setup_dict.get('score')}", path=log_path)
        elif new_verdict == "alert_wait" and prev_verdict not in ("valid_entry", "alert_wait"):
            append_model_alert(
                state,
                kind="forming",
                message=f"Setup forming — {setup_dict.get('side', '').upper()} M{primary_tf} score {setup_dict.get('score')}",
                watch_for="Wait for retest + checklist completion",
                price_zone=[lo, hi],
                timeframe=primary_tf,
            )
        elif not prev_setup and setup_dict:
            append_model_alert(
                state,
                kind="detected",
                message=f"IFVG candidate {setup_dict.get('side', '').upper()} grade {setup_dict.get('grade')} on M{primary_tf}",
                watch_for="Track zone behaviour on approach",
                price_zone=[lo, hi],
                timeframe=primary_tf,
            )
    else:
        append_model_alert(
            state,
            kind="scan",
            message=f"No IFVG setup on M{primary_tf} — continuing watch",
            watch_for="Sweep → displacement → inversion sequence",
            timeframe=primary_tf,
        )

    # Secondary TF glance for model context (reuse cached bars when possible)
    for tf in WATCH_TFS:
        if tf == primary_tf:
            continue
        tb = bars_by_tf.get(tf)
        if tb is None:
            tb, _, _ = _load_bars(tf, count=400)
        if len(tb) < 80:
            continue
        alt = find_ifvg_setups(
            tb,
            macro_frame=macro_frame,
            market_levels=levels,
            news_calendar=calendar,
            openai_config_path=OPENAI_CFG,
            openai_cache_path=RESEARCH_CACHE,
            force_external_research=False,
        )
        if alt and alt[0].verdict == "valid_entry":
            d = setup_to_dict(alt[0], timeframe_minutes=tf)
            append_model_alert(
                state,
                kind="htf_align",
                message=f"M{tf} also shows valid_entry {d.get('side', '').upper()} — confluence",
                watch_for="Consider alignment with primary M15 plan",
                timeframe=tf,
            )

    if brief.get("can_enter"):
        state["status"] = "ready_to_enter"
    elif setup_dict and setup_dict.get("verdict") == "alert_wait":
        state["status"] = "alert_wait"
    elif setup_dict:
        state["status"] = "watching"
    else:
        state["status"] = "scanning"

    save_scout_state(state, state_path)
    scout_log(
        f"scan M{primary_tf} status={state['status']} source={source} "
        f"verdict={setup_dict.get('verdict') if setup_dict else 'none'} "
        f"score={setup_dict.get('score') if setup_dict else '-'}",
        path=log_path,
    )
    return state


def _simple_atr(bars: Sequence[MarketBar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return max(bars[-1].high - bars[-1].low, 1.0) if bars else 10.0
    prev = bars[-period - 1]
    vals = []
    for bar in bars[-period:]:
        vals.append(max(bar.high - bar.low, abs(bar.high - prev.close), abs(bar.low - prev.close)))
        prev = bar
    return sum(vals) / len(vals)


def scout_public_view(state: dict[str, Any] | None, *, timeframe: int | None = None) -> dict[str, Any]:
    """Shape returned to the web UI."""
    st = state or _empty_state()
    setup = st.get("setup")
    if timeframe and setup and int(setup.get("timeframe_minutes") or 0) not in (0, timeframe):
        setup = None
    brief = build_approval_brief(setup, st.get("workflow")) if setup else st.get("approval_brief") or build_approval_brief(None, st.get("workflow"))
    return {
        "status": st.get("status", "unknown"),
        "last_scan_at": st.get("last_scan_at"),
        "next_scan_at": st.get("next_scan_at"),
        "scan_interval_sec": st.get("scan_interval_sec", DEFAULT_SCAN_SECONDS),
        "bridge_online": st.get("bridge_online", False),
        "data_source": st.get("data_source"),
        "openai_configured": st.get("openai_configured", False),
        "setup": setup,
        "workflow": st.get("workflow") or (setup or {}).get("workflow"),
        "approval_brief": brief,
        "model_alerts": list(st.get("model_alerts") or [])[-12:],
        "scan_count": st.get("scan_count", 0),
    }
