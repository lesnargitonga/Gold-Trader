"""Operator workflow — HTF bias → zone → price → LTF confirm → macro → entry → SL → TPs."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..models import MarketBar, Side
from ..strategies import filters as F
from .ifvg_confluence import MarketLevel

MACRO_LOOKBACK_DAYS = 5
LIVE_ALIGNMENT_PREFERRED = "mixed bearish bias"
LIVE_ALIGNMENT_BLOCKERS = frozenset({
    "mixed bullish bias",
    "fully aligned bullish",
    "fully aligned bearish",
    "compression-heavy mixed stack",
})
# Macro regime gate — corrected 2026-06 from the 63-day HTF audit.
# logs/ifvg_htf_edge_audit.json: Grade A is positive in `aligned` (PF 1.78, n=75)
# and `mixed` (thin), and only `opposed` loses (0% WR, n=4). The previous
# macro_regime==mixed REQUIREMENT was overfit on n=1 and blocked the 75 aligned
# winners, driving can_enter≈0 (logs/can_enter_replay.json: current=0 -> macro_fix=49).
# We now block ONLY `opposed`; `partial`/`unavailable` warn (weak macro), not block.
LIVE_MACRO_REGIME_BLOCKERS = frozenset({"opposed"})
LIVE_MACRO_REGIME_WEAK = frozenset({"partial", "unavailable"})

WORKFLOW_FORMULA = (
    "HTF bias → key zone → live price location → 5M/1M confirmation → "
    "macro/options check → entry zone → SL → TP1/TP2/TP3 → invalidation"
)


def compute_htf_bias_for_scout(bars: Sequence[MarketBar]) -> str:
    """Combined 4H+1H bias string for find_ifvg_setups."""
    if not bars:
        return "neutral"
    idx = len(bars) - 1
    b4, _ = _htf_direction(bars, idx, 240)
    b1, _ = _htf_direction(bars, idx, 60)
    combined = _combined_htf_bias(b4, b1)
    return combined if combined in {"bullish", "bearish"} else "neutral"


def _checklist_map(setup: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not setup:
        return {}
    return {str(c.get("name")): dict(c) for c in (setup.get("checklist") or []) if c.get("name")}


def _htf_direction(bars: Sequence[MarketBar], index: int, htf_minutes: int) -> tuple[str, str]:
    """Return bullish / bearish / ranging and a short detail string."""
    ef, es, idx_map = F.htf_ema_aligned(bars, htf_minutes=htf_minutes)
    h = idx_map[min(index, len(idx_map) - 1)]
    if h < 50 or h >= len(ef):
        return "unknown", f"M{htf_minutes // 60}H warming up"
    fast_v, slow_v = ef[h], es[h]
    spread = abs(fast_v - slow_v)
    if spread < max(fast_v * 0.0003, 0.5):
        return "ranging", f"M{htf_minutes // 60}H flat EMA20≈EMA50 ({fast_v:.1f})"
    if fast_v > slow_v:
        return "bullish", f"M{htf_minutes // 60}H EMA20>{slow_v:.1f} — bullish structure"
    return "bearish", f"M{htf_minutes // 60}H EMA20<{slow_v:.1f} — bearish structure"


def alignment_from_bars(bars_by_tf: Mapping[int, Sequence[MarketBar]]) -> str:
    """Bundle-style alignment label from scout watch timeframes (15M · 60M · 4H)."""
    from ..research.analysis import analyze_timeframe_bundle

    datasets: dict[int, Sequence[MarketBar]] = {}
    for tf in (15, 60, 240):
        rows = bars_by_tf.get(tf)
        if rows and len(rows) >= 50:
            datasets[tf] = rows
    if len(datasets) < 2:
        return "unavailable"
    return analyze_timeframe_bundle(datasets).alignment_label


def macro_regime_for_side(
    side: str,
    macro_frame: Any,
    ts: datetime,
    *,
    lookback_days: int = MACRO_LOOKBACK_DAYS,
) -> str:
    """Macro delta alignment vs trade side (same labels as full-stack audit CSV)."""
    if macro_frame is None or not macro_frame.names():
        return "unavailable"

    trade_side = Side.LONG if side.lower() == "long" else Side.SHORT
    deltas: dict[str, float] = {}
    for name in ("dxy", "us10y", "real10y"):
        series = macro_frame.get(name)
        if series is None:
            continue
        chg = series.change(ts, lookback_days)
        if chg is not None:
            deltas[name] = chg
    if not deltas:
        return "partial"

    good = sum(
        1
        for delta in deltas.values()
        if (trade_side is Side.LONG and delta <= 0) or (trade_side is Side.SHORT and delta >= 0)
    )
    if good >= 2:
        return "aligned"
    if good == 1:
        return "mixed"
    return "opposed"


def macro_override_enabled() -> bool:
    """Operator bypass for macro_regime gate (env IFVG_MACRO_OVERRIDE=1)."""
    return os.environ.get("IFVG_MACRO_OVERRIDE", "").strip().lower() in ("1", "true", "yes", "on")


def evaluate_live_sentiment(
    *,
    side: str,
    alignment: str,
    macro_regime: str,
    macro_override: bool | None = None,
) -> dict[str, Any]:
    """Live-profile sentiment gates (corrected 2026-06). Returns blockers + warnings.

    Macro: hard-block ONLY `opposed`. Alignment: ADVISORY only — the old
    requirement of exactly "mixed bearish bias" was overfit (same n=7 era as the
    macro gate) and wrongly blocked e.g. "fully aligned bearish" shorts.
    logs/can_enter_replay.json: demoting alignment lifts can_enter 49 -> 69.
    """
    blockers: list[str] = []
    warnings: list[str] = []
    override = macro_override if macro_override is not None else macro_override_enabled()

    # Alignment is advisory only — never a hard block.
    if alignment not in ("unavailable", LIVE_ALIGNMENT_PREFERRED):
        warnings.append(
            f"Alignment «{alignment}» — audit prefers {LIVE_ALIGNMENT_PREFERRED}; advisory, not a block"
        )

    if macro_regime in LIVE_MACRO_REGIME_BLOCKERS:
        if override:
            warnings.append(
                f"macro_regime={macro_regime} — IFVG_MACRO_OVERRIDE active; "
                "normally blocked (opposed regime, 63-day audit 0% WR n=4)"
            )
        else:
            blockers.append(
                f"macro_regime={macro_regime} — blocked "
                "(63-day audit: opposed 0% WR n=4; set IFVG_MACRO_OVERRIDE=1 to bypass)"
            )
    elif macro_regime in LIVE_MACRO_REGIME_WEAK:
        warnings.append(
            f"macro_regime={macro_regime} — macro confirmation weak; size with caution"
        )
    # aligned / mixed: pass (Grade A positive in both per 63-day audit)

    return {
        "alignment": alignment,
        "macro_regime": macro_regime,
        "macro_override": override,
        "blockers": blockers,
        "warnings": warnings,
    }


def _combined_htf_bias(b4: str, b1: str) -> str:
    if b4 == b1 and b4 in {"bullish", "bearish"}:
        return b4
    if b4 in {"bullish", "bearish"} and b1 == "ranging":
        return b4
    if b1 in {"bullish", "bearish"} and b4 == "ranging":
        return b1
    if b4 == b1:
        return b4
    return "mixed"


def price_in_zone_position(price: float, lo: float, hi: float) -> str:
    if hi <= lo:
        return "middle"
    if price < lo:
        return "below"
    if price > hi:
        return "above"
    span = hi - lo
    rel = (price - lo) / span
    if rel <= 0.33:
        return "bottom"
    if rel >= 0.67:
        return "top"
    return "middle"


def _price_location_favorable(side: str, position: str) -> bool:
    s = side.lower()
    if s == "long":
        return position in {"bottom", "middle", "below"}
    if s == "short":
        return position in {"top", "middle", "above"}
    return False


def _ltf_confirmation(
    bars: Sequence[MarketBar] | None,
    *,
    side: str,
    zone_lo: float,
    zone_hi: float,
    tf_label: str,
) -> tuple[str, str, list[str]]:
    """Scan last few LTF bars for rejection / retest patterns."""
    if not bars or len(bars) < 3:
        return "wait", f"No {tf_label} bars", []
    hits: list[str] = []
    last = bars[-1]
    prev = bars[-2]
    lo, hi = min(zone_lo, zone_hi), max(zone_lo, zone_hi)
    body = max(abs(last.close - last.open), 1e-9)

    if side.lower() == "short":
        if last.high >= lo and last.close < last.open and (last.high - last.close) >= body * 0.4:
            hits.append("rejection candle")
        if prev.close > hi and last.close <= hi and last.high >= hi:
            hits.append("break and retest")
        if last.high > hi and last.close <= hi:
            hits.append("sweep then reclaim")
    else:
        if last.low <= hi and last.close > last.open and (last.close - last.low) >= body * 0.4:
            hits.append("rejection candle")
        if prev.close < lo and last.close >= lo and last.low <= lo:
            hits.append("break and retest")
        if last.low < lo and last.close >= lo:
            hits.append("sweep then reclaim")

    if hits:
        return "pass", f"{tf_label}: " + ", ".join(hits), hits
    if lo <= last.close <= hi:
        return "warn", f"{tf_label}: price in zone — waiting for rejection / retest", []
    return "wait", f"{tf_label}: no LTF confirmation yet", []


def classify_entry_type(
    setup: Mapping[str, Any] | None,
    *,
    price_position: str,
    ltf_status: str,
) -> tuple[str, str]:
    if not setup:
        return "none", "No active setup"
    ck = _checklist_map(setup)
    retest = ck.get("retest_rejection", {})
    inv = ck.get("inversion", {})
    disp = ck.get("displacement", {})
    side = str(setup.get("side") or "")

    if retest.get("status") == "pass" and inv.get("status") == "pass":
        return "pullback", "Pullback / IFVG retest — safest; enter on zone rejection"
    if disp.get("status") == "pass" and price_position in {"above", "below", "top", "bottom"}:
        return "breakout", "Breakdown/breakout — only after candle close + retest"
    if ltf_status != "pass":
        return "wait", "Wait for 5M/1M confirmation before any entry"
    return "aggressive", f"Aggressive {side.upper()} — smaller risk; confirmation still thin"


def _tp_labels(
    plan: Mapping[str, Any],
    side: str,
    levels: Sequence[MarketLevel],
) -> list[dict[str, Any]]:
    labels = [
        ("tp1", "Nearest support/resistance / recent swing"),
        ("tp2", "Previous low/high liquidity"),
        ("tp3", "Round number / configured level / extended target"),
    ]
    out: list[dict[str, Any]] = []
    for key, default_label in labels:
        val = plan.get(key)
        if val is None:
            continue
        label = default_label
        price = float(val)
        for lv in levels:
            if abs(lv.price - price) <= 1.5:
                label = lv.label or f"{lv.kind} @ {lv.price:.0f}"
                break
        out.append({"level": key.upper(), "price": price, "label": label})
    return out


def _step(status: str, title: str, detail: str, *, step: int) -> dict[str, Any]:
    return {"step": step, "title": title, "status": status, "detail": detail}


def build_workflow_context(
    setup: Mapping[str, Any] | None,
    *,
    primary_bars: Sequence[MarketBar],
    current_price: float | None = None,
    bars_by_tf: Mapping[int, Sequence[MarketBar]] | None = None,
    market_levels: Sequence[MarketLevel] = (),
) -> dict[str, Any]:
    """Build the 8-step operator workflow for UI and approval brief."""
    bars_by_tf = bars_by_tf or {}
    price = current_price if current_price is not None else (primary_bars[-1].close if primary_bars else 0.0)
    idx = len(primary_bars) - 1 if primary_bars else 0
    side = str(setup.get("side") or "") if setup else ""
    ck = _checklist_map(setup)

    b4, d4 = _htf_direction(primary_bars, idx, 240)
    b1, d1 = _htf_direction(primary_bars, idx, 60)
    combined = _combined_htf_bias(b4, b1)

    htf_status = "wait"
    htf_detail = f"4H {b4} · 1H {b1}"
    if combined in {"bullish", "bearish"}:
        if setup and side:
            aligned = (combined == "bullish" and side.lower() == "long") or (
                combined == "bearish" and side.lower() == "short"
            )
            htf_status = "pass" if aligned else "fail"
            htf_detail = f"{d4} · {d1}"
            if not aligned:
                htf_detail += f" — avoid {side.upper()} into {combined} HTF"
        else:
            htf_status = "pass" if combined != "mixed" else "warn"
            htf_detail = f"{d4} · {d1} — bias {combined}"
    elif combined == "ranging":
        htf_status = "warn"
        htf_detail = f"{d4} · {d1} — ranging; pick edges only"

    zone_lo = zone_hi = 0.0
    zone_detail = "Scanning M15/M5 for IFVG · prior highs/lows · S/R · round numbers"
    zone_status = "wait"
    if setup:
        zone = setup.get("zone") or {}
        zone_lo, zone_hi = float(zone.get("bot", 0)), float(zone.get("top", 0))
        tf = setup.get("timeframe_minutes") or 15
        zone_status = "pass"
        zone_detail = f"M{tf} IFVG {min(zone_lo, zone_hi):.2f}–{max(zone_lo, zone_hi):.2f}"
        near_levels = [
            lv.label or f"{lv.kind}@{lv.price:.0f}"
            for lv in market_levels
            if abs(lv.price - price) <= max(abs(zone_hi - zone_lo), 8.0)
        ][:3]
        if near_levels:
            zone_detail += f" · nearby: {', '.join(near_levels)}"

    pos = price_in_zone_position(price, min(zone_lo, zone_hi), max(zone_lo, zone_hi)) if setup else "outside"
    pos_status = "wait"
    pos_detail = f"Price {price:.2f} — no active zone"
    if setup:
        pos_detail = f"Price {price:.2f} at zone {pos}"
        favorable = _price_location_favorable(side, pos)
        if side.lower() == "long" and pos == "top":
            pos_status = "fail"
            pos_detail += " — at resistance edge; avoid buying market"
        elif side.lower() == "short" and pos == "bottom":
            pos_status = "fail"
            pos_detail += " — at support edge; avoid selling market"
        elif favorable:
            pos_status = "pass"
        else:
            pos_status = "warn"

    m5 = bars_by_tf.get(5)
    m1 = bars_by_tf.get(1)
    ltf_hits: list[str] = []
    ltf_status, ltf_detail = "wait", "Waiting for 5M/1M confirmation"
    if setup:
        s5, d5, h5 = _ltf_confirmation(m5, side=side, zone_lo=zone_lo, zone_hi=zone_hi, tf_label="M5")
        s1, d1c, h1 = _ltf_confirmation(m1, side=side, zone_lo=zone_lo, zone_hi=zone_hi, tf_label="M1")
        ltf_hits = h5 + h1
        retest_ck = ck.get("retest_rejection", {})
        if retest_ck.get("status") == "pass":
            ltf_status = "pass"
            ltf_detail = retest_ck.get("reason") or d5
            if "IFVG retest" not in ltf_detail:
                ltf_detail = f"IFVG retest · {ltf_detail}"
        elif s5 == "pass" or s1 == "pass":
            ltf_status = "pass"
            ltf_detail = d5 if s5 == "pass" else d1c
        elif s5 == "warn" or s1 == "warn":
            ltf_status = "warn"
            ltf_detail = d5
        else:
            ltf_status = "wait"
            ltf_detail = d5

    ext = (setup or {}).get("external_research") or {}
    grading_ctx = (setup or {}).get("grading") or {}
    research_mode = str(ext.get("mode") or grading_ctx.get("research_mode") or "soft")
    macro_ck = ck.get("macro_confirmation", {})
    ext_status = "wait"
    ext_detail = "DXY · US10Y · futures · news · CME/options — grades quality (soft mode never blocks)"
    if setup:
        if ext.get("enabled"):
            macro = ext.get("macro") or {}
            opts = ext.get("options") or {}
            parts = [
                f"DXY {macro.get('dxy_bias', '?')}",
                f"US10Y {macro.get('us10y_bias', '?')}",
                f"options {opts.get('bias', '?')}",
            ]
            if ext.get("should_block_trade"):
                ext_status = "fail" if research_mode == "hard" else "warn"
            elif ext.get("supports_trade") and int(ext.get("confidence") or 0) >= 55:
                ext_status = "pass"
            elif macro_ck.get("status") == "pass":
                ext_status = "pass"
            else:
                ext_status = "warn"
            ext_detail = " · ".join(parts)
            if ext.get("news_risk") == "high":
                ext_status = "fail" if research_mode == "hard" else "warn"
                ext_detail += " · high news risk (grade reduced)"
        elif macro_ck:
            ext_status = macro_ck.get("status") or "warn"
            ext_detail = macro_ck.get("reason") or ext_detail

    entry_type, entry_detail = classify_entry_type(setup, price_position=pos, ltf_status=ltf_status)

    plan = (setup or {}).get("entry_plan") or {}
    inv = plan.get("invalidation") or plan.get("stop")
    inv_status = "wait"
    inv_detail = "Define invalidation before entry"
    if inv is not None:
        inv_status = "pass"
        inv_detail = f"Invalid if close beyond {float(inv):.2f} (stop {float(plan.get('stop', inv)):.2f})"

    tp_rows = _tp_labels(plan, side, market_levels) if plan else []
    tp_status = "pass" if tp_rows else "wait"
    tp_detail = " · ".join(f"{r['level']} {r['price']:.2f} ({r['label']})" for r in tp_rows) or (
        "TPs from swings + market_levels — TP1 min 1R floor; TP2/TP3 structural (no fixed 2R runner cap)"
    )

    steps = [
        _step(htf_status, "HTF bias (4H · 1H)", htf_detail, step=1),
        _step(zone_status, "Intraday zone (15M · 5M)", zone_detail, step=2),
        _step(pos_status, "Live price location", pos_detail, step=3),
        _step(ltf_status, "5M · 1M confirmation", ltf_detail, step=4),
        _step(ext_status, "Macro · options · news", ext_detail, step=5),
        _step("pass" if entry_type == "pullback" else ("warn" if entry_type == "aggressive" else "wait"), "Entry type", f"{entry_type}: {entry_detail}", step=6),
        _step(inv_status, "Invalidation", inv_detail, step=7),
        _step(tp_status, "TP1 · TP2 · TP3 @ liquidity", tp_detail, step=8),
    ]

    blockers = [s["detail"] for s in steps if s["status"] == "fail"]
    warnings = [s["detail"] for s in steps if s["status"] == "warn"]
    passes = sum(1 for s in steps if s["status"] == "pass")
    letter = str(grading_ctx.get("letter") or "")
    hard_blocked = bool((setup or {}).get("externally_blocked")) and research_mode == "hard"
    # Eligibility is per-slice (grade x timeframe) via config/tp_routing.json, not
    # hard-coded grade A. Lets Grade B be paper-ready on 4H only, etc.
    from .tp_routing import resolve_tp_route
    tf_min = int((setup or {}).get("timeframe_minutes") or 0)
    route_eligible = bool(resolve_tp_route(letter, tf_min).get("eligible")) if setup else False
    workflow_ready = (
        bool(setup)
        and setup.get("verdict") in ("valid_entry", "alert_wait")
        and route_eligible
        and not hard_blocked
    )
    if blockers:
        workflow_ready = False

    return {
        "formula": WORKFLOW_FORMULA,
        "htf_bias": {"h4": b4, "h1": b1, "combined": combined},
        "price_position": pos,
        "entry_type": entry_type,
        "entry_type_detail": entry_detail,
        "ltf_signals": ltf_hits,
        "steps": steps,
        "passes": passes,
        "total_steps": len(steps),
        "blockers": blockers,
        "warnings": warnings,
        "workflow_ready": workflow_ready,
    }
