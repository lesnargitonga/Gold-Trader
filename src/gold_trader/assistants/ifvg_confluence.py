"""Strict IFVG confluence assistant for manual gold execution.

This module is intentionally operator-facing. It does not place orders and it
does not try to discover a new edge. It turns IFVG candidates into a structured
checklist and entry plan so the UI, agent-cycle, and shadow journal all read
the same decision.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from ..calendar import NewsCalendar
from ..models import MarketBar, Side
from .ifvg_grading import apply_soft_mode_external, compute_setup_grading
from ..strategies import filters as F

if TYPE_CHECKING:
    from ..research.realtime_research import OpenAIResearchConfig, RealtimeResearchResult


@dataclass(frozen=True)
class MarketLevel:
    price: float
    kind: str
    label: str = ""
    strength: float = 1.0


@dataclass(frozen=True)
class ChecklistItem:
    name: str
    label: str
    points: int
    max_points: int
    status: str
    reason: str


@dataclass(frozen=True)
class IFVGCandidate:
    side: Side
    formation_idx: int
    impulse_idx: int
    inversion_idx: int
    signal_idx: int
    gap_bot: float
    gap_top: float
    sweep_idx: int | None
    sweep_level: float | None
    source: str


@dataclass(frozen=True)
class IFVGEntryPlan:
    entry_low: float
    entry_high: float
    entry: float
    stop: float
    tp1: float
    tp2: float
    tp3: float
    invalidation: float


@dataclass(frozen=True)
class IFVGSetup:
    candidate: IFVGCandidate
    score: int
    grade: str
    verdict: str
    checklist: tuple[ChecklistItem, ...]
    plan: IFVGEntryPlan
    warnings: tuple[str, ...] = ()
    manual_approval_required: bool = True
    external_research: RealtimeResearchResult | None = None
    external_research_mode: str = "off"
    externally_blocked: bool = False


@dataclass(frozen=True)
class IFVGAssistantConfig:
    atr_period: int = 14
    fvg_lookback: int = 40
    inversion_lookback: int = 20
    retest_lookback: int = 10
    sweep_lookback: int = 24
    pivot_window: int = 2
    min_gap_atr: float = 0.08
    full_displacement_atr: float = 1.5
    partial_displacement_atr: float = 1.0
    stop_buffer_atr: float = 0.15
    htf_minutes: int = 240
    htf_fast: int = 20
    htf_slow: int = 50
    macro_lookback_days: int = 5
    level_danger_atr: float = 0.75
    valid_threshold: int = 80
    alert_threshold: int = 65


def find_ifvg_setups(
    bars: Sequence[MarketBar],
    *,
    index: int | None = None,
    config: IFVGAssistantConfig | None = None,
    macro_frame: Any = None,
    market_levels: Sequence[MarketLevel] = (),
    news_calendar: NewsCalendar | None = None,
    higher_timeframe_bias: str | None = None,
    openai_research_config: OpenAIResearchConfig | None = None,
    openai_config_path: Path | str = "config/openai_research.json",
    openai_cache_path: Path | str = "data/cache/openai_market_research.json",
    force_external_research: bool = False,
    scout_alerts: Sequence[dict[str, Any]] | None = None,
) -> list[IFVGSetup]:
    """Find strict IFVG retests at *index* and return scored setup plans."""
    cfg = config or IFVGAssistantConfig()
    if not bars:
        return []
    idx = len(bars) - 1 if index is None else index
    if idx < max(cfg.atr_period + 2, 5):
        return []
    atr = _atr(bars, idx, cfg.atr_period)
    if atr <= 0:
        return []

    candidates = _scan_candidates(bars, idx, cfg, atr)
    setups = [
        evaluate_ifvg_confluence(
            bars,
            candidate,
            config=cfg,
            macro_frame=macro_frame,
            market_levels=market_levels,
            news_calendar=news_calendar,
            higher_timeframe_bias=higher_timeframe_bias,
            openai_research_config=openai_research_config,
            openai_config_path=openai_config_path,
            openai_cache_path=openai_cache_path,
            force_external_research=force_external_research,
            scout_alerts=scout_alerts,
        )
        for candidate in candidates
    ]
    setups.sort(key=lambda setup: (setup.score, setup.candidate.formation_idx), reverse=True)
    return setups


def evaluate_ifvg_confluence(
    bars: Sequence[MarketBar],
    candidate: IFVGCandidate,
    *,
    config: IFVGAssistantConfig | None = None,
    macro_frame: Any = None,
    market_levels: Sequence[MarketLevel] = (),
    news_calendar: NewsCalendar | None = None,
    higher_timeframe_bias: str | None = None,
    openai_research_config: OpenAIResearchConfig | None = None,
    openai_config_path: Path | str = "config/openai_research.json",
    openai_cache_path: Path | str = "data/cache/openai_market_research.json",
    force_external_research: bool = False,
    scout_alerts: Sequence[dict[str, Any]] | None = None,
) -> IFVGSetup:
    cfg = config or IFVGAssistantConfig()
    idx = candidate.signal_idx
    atr = _atr(bars, idx, cfg.atr_period)
    checklist = (
        _liquidity_sweep_item(candidate),
        _displacement_item(bars, candidate, atr, cfg),
        _inversion_item(bars, candidate),
        _retest_item(bars, candidate),
        _htf_item(bars, candidate, atr, cfg, higher_timeframe_bias),
        _macro_item(bars, candidate, macro_frame, cfg),
        _level_item(bars, candidate, atr, market_levels, cfg),
    )
    warnings = list(_news_warnings(bars[idx], news_calendar))
    warnings.extend(_item_warnings(checklist))
    score = max(0, min(100, sum(item.points for item in checklist)))
    plan = _entry_plan(bars, candidate, atr, market_levels, cfg)
    if score >= cfg.valid_threshold:
        grade = "A"
        verdict = "valid_entry"
    elif score >= cfg.alert_threshold:
        grade = "B"
        verdict = "alert_wait"
    else:
        grade = "C"
        verdict = "ignore"
    external = None
    external_mode = "off"
    externally_blocked = False
    research_config = openai_research_config
    if research_config is None:
        from ..research.realtime_research import load_openai_research_config
        research_config = load_openai_research_config(openai_config_path)
    external_mode = research_config.mode
    if score >= research_config.min_ifvg_score_to_research:
        external = _run_external_research(
            bars=bars,
            candidate=candidate,
            checklist=checklist,
            plan=plan,
            score=score,
            market_levels=market_levels,
            research_config=research_config,
            config_path=openai_config_path,
            cache_path=openai_cache_path,
            force_refresh=force_external_research,
            scout_alerts=scout_alerts,
        )
        externally_blocked, warnings = apply_soft_mode_external(
            external=external,
            research_config=research_config,
            side=candidate.side.value,
            warnings=list(warnings),
        )
        if externally_blocked:
            verdict = "externally_blocked"
    return IFVGSetup(
        candidate=candidate,
        score=score,
        grade=grade,
        verdict=verdict,
        checklist=checklist,
        plan=plan,
        warnings=tuple(warnings),
        external_research=external,
        external_research_mode=external_mode,
        externally_blocked=externally_blocked,
    )


def load_market_levels(path: Path | str) -> list[MarketLevel]:
    """Load manually maintained support/resistance/options levels.

    Missing files are neutral. Supported shape:
    {"levels": [{"price": 4400, "kind": "resistance", "label": "4400 OI"}]}
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
    except Exception:
        return []
    rows = raw.get("levels", raw if isinstance(raw, list) else [])
    levels: list[MarketLevel] = []
    for row in rows:
        try:
            levels.append(MarketLevel(
                price=float(row["price"]),
                kind=str(row.get("kind") or "level").strip().lower(),
                label=str(row.get("label") or ""),
                strength=float(row.get("strength") or 1.0),
            ))
        except (TypeError, ValueError, KeyError):
            continue
    return levels


def record_shadow_setup(path: Path | str, setup: IFVGSetup, *, timeframe_minutes: int) -> None:
    """Append a setup snapshot for later manual outcome calibration."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    row = _shadow_row(setup, timeframe_minutes)
    exists = p.exists()
    with p.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def _trade_style(timeframe_minutes: int | None) -> str:
    tf = int(timeframe_minutes or 15)
    if tf <= 5:
        return "scalping"
    if tf <= 15:
        return "intraday"
    if tf <= 60:
        return "swing"
    return "position"


def _ai_verdict(setup: IFVGSetup, *, timeframe_minutes: int | None, grading: dict[str, Any] | None = None) -> str:
    side = setup.candidate.side.value.upper()
    style = _trade_style(timeframe_minutes)
    g = grading or {}
    final = g.get("letter", setup.grade)
    if setup.externally_blocked:
        return f"BLOCKED (hard mode) — external research conflicts with this {side} {style} plan."
    if setup.verdict == "valid_entry":
        base = f"TAKE — {side} {style} IFVG · grade {final} (tech {setup.score}/100)."
    elif setup.verdict == "alert_wait":
        base = f"WAIT — {side} {style} IFVG forming · grade {final} (tech {setup.score}/100)."
    else:
        base = f"SKIP — no tradable {side} IFVG on this timeframe (tech {setup.score}/100)."
    if g.get("action"):
        base = f"{base} {g['action']}"
    ext = setup.external_research
    if ext is None or setup.external_research_mode == "off":
        return base
    if setup.external_research_mode == "soft":
        if ext.supports_trade and ext.confidence >= 60:
            return f"{base} External context supportive (confidence {ext.confidence}/100)."
        if ext.news_risk == "high":
            return f"{base} External warning: high news risk — grade reduced, not removed."
        if ext.should_block_trade:
            return f"{base} External context mixed — use grade/risk, not a hard block."
    if ext.supports_trade and ext.confidence >= 60:
        return f"{base} Web research supports the trade (confidence {ext.confidence}/100)."
    if ext.should_block_trade or ext.news_risk == "high":
        return f"{base} Web research warns ({ext.summary or 'elevated risk'})."
    if ext.confidence:
        return f"{base} Web research is mixed (confidence {ext.confidence}/100)."
    return base


def setup_to_dict(setup: IFVGSetup, *, timeframe_minutes: int | None = None) -> dict[str, Any]:
    from ..research.realtime_research import OpenAIResearchConfig, result_to_dict

    c = setup.candidate
    plan = setup.plan
    research_config = OpenAIResearchConfig(
        enabled=setup.external_research_mode != "off",
        mode=setup.external_research_mode,
    )
    grading = compute_setup_grading(
        setup.score,
        setup.external_research,
        research_config=research_config,
    )
    out = {
        "timeframe_minutes": timeframe_minutes,
        "trade_style": _trade_style(timeframe_minutes),
        "ai_verdict": _ai_verdict(setup, timeframe_minutes=timeframe_minutes, grading=grading),
        "side": c.side.value,
        "trade_action": "buy" if c.side is Side.LONG else "sell",
        "source": c.source,
        "inversion_note": (
            "Former bullish FVG inverted — zone is now resistance (SELL on retest)"
            if c.source == "inverted_bullish_fvg"
            else "Former bearish FVG inverted — zone is now support (BUY on retest)"
            if c.source == "inverted_bearish_fvg"
            else ""
        ),
        "score": setup.score,
        "technical_grade": setup.grade,
        "grade": grading["letter"],
        "grading": grading,
        "verdict": setup.verdict,
        "manual_approval_required": setup.manual_approval_required,
        "zone": {"bot": c.gap_bot, "top": c.gap_top},
        "sweep": {"index": c.sweep_idx, "level": c.sweep_level},
        "indices": {
            "formation": c.formation_idx,
            "impulse": c.impulse_idx,
            "inversion": c.inversion_idx,
            "signal": c.signal_idx,
        },
        "entry_plan": {
            "entry_low": plan.entry_low,
            "entry_high": plan.entry_high,
            "entry": plan.entry,
            "stop": plan.stop,
            "tp1": plan.tp1,
            "tp2": plan.tp2,
            "tp3": plan.tp3,
            "invalidation": plan.invalidation,
        },
        "checklist": [
            {
                "name": item.name,
                "label": item.label,
                "points": item.points,
                "max_points": item.max_points,
                "status": item.status,
                "reason": item.reason,
            }
            for item in setup.checklist
        ],
        "warnings": list(setup.warnings),
        "external_research": (
            result_to_dict(
                setup.external_research,
                enabled=setup.external_research is not None and setup.external_research_mode != "off",
                mode=setup.external_research_mode,
            )
            if setup.external_research is not None
            else {
                "enabled": False,
                "mode": setup.external_research_mode,
                "last_checked": None,
                "bias": "unknown",
                "supports_trade": False,
                "should_block_trade": False,
                "confidence": 0,
                "news_risk": "unknown",
                "macro": {"dxy_bias": "unknown", "us10y_bias": "unknown", "real_yield_bias": "unknown"},
                "options": {"bias": "unknown", "important_levels": [], "danger_zones": [], "notes": ""},
                "warnings": [],
                "summary": "",
                "sources": [],
            }
        ),
        "externally_blocked": setup.externally_blocked,
    }
    return out


def _run_external_research(
    *,
    bars: Sequence[MarketBar],
    candidate: IFVGCandidate,
    checklist: Sequence[ChecklistItem],
    plan: IFVGEntryPlan,
    score: int,
    market_levels: Sequence[MarketLevel],
    research_config: OpenAIResearchConfig,
    config_path: Path | str,
    cache_path: Path | str,
    force_refresh: bool,
    scout_alerts: Sequence[dict[str, Any]] | None = None,
) -> Any:
    from ..research.realtime_research import run_openai_market_research

    return run_openai_market_research(
        symbol="XAUUSD",
        side="buy" if candidate.side is Side.LONG else "sell",
        current_price=bars[candidate.signal_idx].close,
        ifvg_zone_low=candidate.gap_bot,
        ifvg_zone_high=candidate.gap_top,
        entry_low=plan.entry_low,
        entry_high=plan.entry_high,
        stop_loss=plan.stop,
        tp1=plan.tp1,
        tp2=plan.tp2,
        tp3=plan.tp3,
        technical_score=score,
        checklist_rows=[item.__dict__ for item in checklist],
        market_levels=[level.__dict__ for level in market_levels],
        config_path=config_path,
        cache_path=cache_path,
        force_refresh=force_refresh,
        scout_alerts=scout_alerts,
    )


def _scan_candidates(
    bars: Sequence[MarketBar],
    index: int,
    cfg: IFVGAssistantConfig,
    atr: float,
) -> list[IFVGCandidate]:
    min_gap = cfg.min_gap_atr * atr
    scan_start = max(2, index - cfg.fvg_lookback - cfg.inversion_lookback)
    out: list[IFVGCandidate] = []
    for k in range(index - 1, scan_start - 1, -1):
        if k - 2 < 0:
            break
        b_prev2 = bars[k - 2]
        b_curr = bars[k]
        if b_prev2.high < b_curr.low:
            gap_bot, gap_top = b_prev2.high, b_curr.low
            if gap_top - gap_bot < min_gap:
                continue
            inv_idx = _find_inversion(bars, k, index, level=gap_bot, direction="below", max_steps=cfg.inversion_lookback)
            if inv_idx is None or index - inv_idx > cfg.retest_lookback:
                continue
            bar = bars[index]
            if bar.high >= gap_bot and bar.close <= gap_bot:
                sweep_idx, sweep_level = _sequence_sweep(bars, Side.SHORT, k, cfg)
                out.append(IFVGCandidate(
                    side=Side.SHORT, formation_idx=k, impulse_idx=k - 1,
                    inversion_idx=inv_idx, signal_idx=index, gap_bot=gap_bot,
                    gap_top=gap_top, sweep_idx=sweep_idx, sweep_level=sweep_level,
                    source="inverted_bullish_fvg",
                ))
        elif b_prev2.low > b_curr.high:
            gap_bot, gap_top = b_curr.high, b_prev2.low
            if gap_top - gap_bot < min_gap:
                continue
            inv_idx = _find_inversion(bars, k, index, level=gap_top, direction="above", max_steps=cfg.inversion_lookback)
            if inv_idx is None or index - inv_idx > cfg.retest_lookback:
                continue
            bar = bars[index]
            if bar.low <= gap_top and bar.close >= gap_top:
                sweep_idx, sweep_level = _sequence_sweep(bars, Side.LONG, k, cfg)
                out.append(IFVGCandidate(
                    side=Side.LONG, formation_idx=k, impulse_idx=k - 1,
                    inversion_idx=inv_idx, signal_idx=index, gap_bot=gap_bot,
                    gap_top=gap_top, sweep_idx=sweep_idx, sweep_level=sweep_level,
                    source="inverted_bearish_fvg",
                ))
    return out


def _find_inversion(
    bars: Sequence[MarketBar],
    formation_idx: int,
    current_idx: int,
    *,
    level: float,
    direction: str,
    max_steps: int,
) -> int | None:
    end = min(current_idx, formation_idx + max_steps)
    for j in range(formation_idx + 1, end + 1):
        if direction == "below" and bars[j].close < level:
            return j
        if direction == "above" and bars[j].close > level:
            return j
    return None


def _sequence_sweep(
    bars: Sequence[MarketBar],
    side: Side,
    formation_idx: int,
    cfg: IFVGAssistantConfig,
) -> tuple[int | None, float | None]:
    start = max(cfg.pivot_window, formation_idx - cfg.sweep_lookback)
    end = max(start, formation_idx - 1)
    for pivot_idx in range(end, start - 1, -1):
        if side is Side.LONG and _is_pivot_low(bars, pivot_idx, cfg.pivot_window):
            level = bars[pivot_idx].low
            for j in range(pivot_idx + cfg.pivot_window, formation_idx):
                if bars[j].low < level and bars[j].close > level:
                    return j, level
        if side is Side.SHORT and _is_pivot_high(bars, pivot_idx, cfg.pivot_window):
            level = bars[pivot_idx].high
            for j in range(pivot_idx + cfg.pivot_window, formation_idx):
                if bars[j].high > level and bars[j].close < level:
                    return j, level
    return None, None


def _liquidity_sweep_item(candidate: IFVGCandidate) -> ChecklistItem:
    if candidate.sweep_idx is not None and candidate.sweep_idx < candidate.formation_idx:
        return ChecklistItem("liquidity_sweep", "Liquidity sweep before IFVG", 20, 20, "pass", f"sweep@{candidate.sweep_idx}")
    return ChecklistItem("liquidity_sweep", "Liquidity sweep before IFVG", 0, 20, "fail", "no sequence-valid sweep before formation")


def _displacement_item(
    bars: Sequence[MarketBar],
    candidate: IFVGCandidate,
    atr: float,
    cfg: IFVGAssistantConfig,
) -> ChecklistItem:
    impulse = bars[candidate.impulse_idx]
    body = abs(impulse.close - impulse.open)
    ratio = body / atr if atr > 0 else 0.0
    aligned = (
        (candidate.side is Side.LONG and impulse.close < impulse.open)
        or (candidate.side is Side.SHORT and impulse.close > impulse.open)
    )
    if aligned and ratio >= cfg.full_displacement_atr:
        return ChecklistItem("displacement", "Strong displacement", 15, 15, "pass", f"body={ratio:.2f}*atr")
    if aligned and ratio >= cfg.partial_displacement_atr:
        return ChecklistItem("displacement", "Strong displacement", 8, 15, "partial", f"body={ratio:.2f}*atr")
    return ChecklistItem("displacement", "Strong displacement", 0, 15, "fail", f"weak_or_wrong_way body={ratio:.2f}*atr")


def _inversion_item(bars: Sequence[MarketBar], candidate: IFVGCandidate) -> ChecklistItem:
    inv = bars[candidate.inversion_idx]
    if candidate.side is Side.SHORT and inv.close < candidate.gap_bot:
        return ChecklistItem("inversion", "Clean IFVG inversion", 20, 20, "pass", f"closed below {candidate.gap_bot:.2f}")
    if candidate.side is Side.LONG and inv.close > candidate.gap_top:
        return ChecklistItem("inversion", "Clean IFVG inversion", 20, 20, "pass", f"closed above {candidate.gap_top:.2f}")
    return ChecklistItem("inversion", "Clean IFVG inversion", 0, 20, "fail", "no close beyond inverted edge")


def _retest_item(bars: Sequence[MarketBar], candidate: IFVGCandidate) -> ChecklistItem:
    bar = bars[candidate.signal_idx]
    body = max(abs(bar.close - bar.open), 1e-9)
    if candidate.side is Side.SHORT and bar.high >= candidate.gap_bot and bar.close <= candidate.gap_bot:
        wick = bar.high - bar.close
        if wick >= body * 0.5:
            return ChecklistItem("retest_rejection", "Retest gives rejection", 15, 15, "pass", f"upper_wick={wick:.2f}")
        return ChecklistItem("retest_rejection", "Retest gives rejection", 7, 15, "partial", f"weak_upper_wick={wick:.2f}")
    if candidate.side is Side.LONG and bar.low <= candidate.gap_top and bar.close >= candidate.gap_top:
        wick = bar.close - bar.low
        if wick >= body * 0.5:
            return ChecklistItem("retest_rejection", "Retest gives rejection", 15, 15, "pass", f"lower_wick={wick:.2f}")
        return ChecklistItem("retest_rejection", "Retest gives rejection", 7, 15, "partial", f"weak_lower_wick={wick:.2f}")
    return ChecklistItem("retest_rejection", "Retest gives rejection", 0, 15, "fail", "no retest rejection")


def _htf_item(
    bars: Sequence[MarketBar],
    candidate: IFVGCandidate,
    atr: float,
    cfg: IFVGAssistantConfig,
    higher_timeframe_bias: str | None,
) -> ChecklistItem:
    bias = (higher_timeframe_bias or "").lower()
    if bias:
        if bias == "bullish" and candidate.side is Side.LONG:
            return ChecklistItem("htf_agreement", "HTF direction agrees", 15, 15, "pass", "bundle_bias=bullish")
        if bias == "bearish" and candidate.side is Side.SHORT:
            return ChecklistItem("htf_agreement", "HTF direction agrees", 15, 15, "pass", "bundle_bias=bearish")
        if bias in {"bullish", "bearish"}:
            return ChecklistItem("htf_agreement", "HTF direction agrees", 0, 15, "fail", f"counter_bundle_bias={bias}")
    ok, reason = F.htf_trend_aligned(
        bars, candidate.signal_idx, candidate.side,
        htf_minutes=cfg.htf_minutes, fast=cfg.htf_fast, slow=cfg.htf_slow,
    )
    if ok:
        return ChecklistItem("htf_agreement", "HTF direction agrees", 15, 15, "pass", reason)
    if "warmup" in reason:
        return ChecklistItem("htf_agreement", "HTF direction agrees", 7, 15, "partial", reason)
    return ChecklistItem("htf_agreement", "HTF direction agrees", 0, 15, "fail", reason)


def _macro_item(
    bars: Sequence[MarketBar],
    candidate: IFVGCandidate,
    macro_frame: Any,
    cfg: IFVGAssistantConfig,
) -> ChecklistItem:
    if macro_frame is None or not getattr(macro_frame, "names", lambda: [])():
        return ChecklistItem("macro_confirmation", "DXY/yields confirm", 5, 10, "partial", "macro_unavailable_neutral")
    ts = bars[candidate.signal_idx].timestamp
    checks: list[str] = []
    points = 0
    for name in ("dxy", "us10y", "real10y"):
        series = macro_frame.get(name) if hasattr(macro_frame, "get") else None
        if series is None:
            continue
        delta = series.change(ts, cfg.macro_lookback_days)
        if delta is None:
            continue
        good = (candidate.side is Side.SHORT and delta >= 0) or (candidate.side is Side.LONG and delta <= 0)
        checks.append(f"{name}={delta:+.3f}")
        points += 1 if good else -1
    if not checks:
        return ChecklistItem("macro_confirmation", "DXY/yields confirm", 5, 10, "partial", "macro_series_missing_neutral")
    if points >= 2:
        return ChecklistItem("macro_confirmation", "DXY/yields confirm", 10, 10, "pass", ",".join(checks))
    if points >= 0:
        return ChecklistItem("macro_confirmation", "DXY/yields confirm", 5, 10, "partial", ",".join(checks))
    return ChecklistItem("macro_confirmation", "DXY/yields confirm", 0, 10, "fail", ",".join(checks))


def _level_item(
    bars: Sequence[MarketBar],
    candidate: IFVGCandidate,
    atr: float,
    market_levels: Sequence[MarketLevel],
    cfg: IFVGAssistantConfig,
) -> ChecklistItem:
    if not market_levels:
        return ChecklistItem("not_into_sr", "Not trading into support/resistance", 3, 5, "partial", "levels_unavailable_neutral")
    price = bars[candidate.signal_idx].close
    danger = max(atr * cfg.level_danger_atr, 0.5)
    ahead = []
    for level in market_levels:
        if candidate.side is Side.LONG and level.price > price and (level.price - price) <= danger:
            ahead.append(level)
        if candidate.side is Side.SHORT and level.price < price and (price - level.price) <= danger:
            ahead.append(level)
    if ahead:
        labels = ",".join(level.label or f"{level.kind}@{level.price:.2f}" for level in ahead[:3])
        return ChecklistItem("not_into_sr", "Not trading into support/resistance", 0, 5, "fail", f"nearby_level_ahead:{labels}")
    return ChecklistItem("not_into_sr", "Not trading into support/resistance", 5, 5, "pass", "no nearby configured level ahead")


def _levels_beyond(
    levels: Sequence[MarketLevel],
    price: float,
    side: Side,
) -> list[float]:
    if side is Side.SHORT:
        return sorted((lv.price for lv in levels if lv.price < price), reverse=True)
    return sorted(lv.price for lv in levels if lv.price > price)


def _entry_plan(
    bars: Sequence[MarketBar],
    candidate: IFVGCandidate,
    atr: float,
    market_levels: Sequence[MarketLevel],
    cfg: IFVGAssistantConfig,
) -> IFVGEntryPlan:
    entry_low, entry_high = candidate.gap_bot, candidate.gap_top
    entry = bars[candidate.signal_idx].close
    buffer = max(cfg.stop_buffer_atr * atr, 0.1)
    if candidate.side is Side.SHORT:
        sweep_stop = (candidate.sweep_level or candidate.gap_top) + buffer
        stop = max(candidate.gap_top + buffer, sweep_stop)
        risk = max(stop - entry, buffer)
        lows = _recent_lows(bars, candidate.signal_idx)
        level_targets = _levels_beyond(market_levels, entry, Side.SHORT)
        floor1 = entry - risk
        swing1 = _nearest_below(lows, entry)
        tp1 = min(swing1, floor1) if swing1 is not None else floor1
        tp2 = _nearest_below(lows, tp1 - 1e-9)
        if tp2 is None:
            beyond = [p for p in level_targets if p < tp1 - 1e-9]
            tp2 = beyond[0] if beyond else None
        if tp2 is None:
            tp2 = tp1 - risk
        tp3 = _nearest_below(lows, tp2 - 1e-9)
        if tp3 is None:
            beyond = [p for p in level_targets if p < tp2 - 1e-9]
            tp3 = beyond[0] if beyond else (tp2 - risk)
    else:
        sweep_stop = (candidate.sweep_level or candidate.gap_bot) - buffer
        stop = min(candidate.gap_bot - buffer, sweep_stop)
        risk = max(entry - stop, buffer)
        highs = _recent_highs(bars, candidate.signal_idx)
        level_targets = _levels_beyond(market_levels, entry, Side.LONG)
        floor1 = entry + risk
        swing1 = _nearest_above(highs, entry)
        tp1 = max(swing1, floor1) if swing1 is not None else floor1
        tp2 = _nearest_above(highs, tp1 + 1e-9)
        if tp2 is None:
            beyond = [p for p in level_targets if p > tp1 + 1e-9]
            tp2 = beyond[0] if beyond else None
        if tp2 is None:
            tp2 = tp1 + risk
        tp3 = _nearest_above(highs, tp2 + 1e-9)
        if tp3 is None:
            beyond = [p for p in level_targets if p > tp2 + 1e-9]
            tp3 = beyond[0] if beyond else (tp2 + risk)
    return IFVGEntryPlan(
        entry_low=entry_low,
        entry_high=entry_high,
        entry=entry,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        invalidation=stop,
    )


def _news_warnings(bar: MarketBar, calendar: NewsCalendar | None) -> tuple[str, ...]:
    warnings: list[str] = []
    if calendar is not None:
        blocked, event = calendar.is_blackout(bar.timestamp, window_minutes=30)
        if blocked and event is not None:
            warnings.append(f"news_blackout:{event.event}@{event.timestamp.isoformat()}")
    ok, reason = F.news_clear(bar, min_minutes=30)
    if not ok:
        warnings.append(reason)
    return tuple(warnings)


def _item_warnings(items: Sequence[ChecklistItem]) -> list[str]:
    return [f"{item.name}:{item.reason}" for item in items if item.status == "fail"]


def _atr(bars: Sequence[MarketBar], index: int, period: int) -> float:
    if index <= 0:
        return 0.0
    start = max(1, index - period + 1)
    trs = []
    for i in range(start, index + 1):
        trs.append(bars[i].true_range(bars[i - 1].close))
    return sum(trs) / len(trs) if trs else 0.0


def _is_pivot_low(bars: Sequence[MarketBar], i: int, w: int) -> bool:
    if i - w < 0 or i + w >= len(bars):
        return False
    lo = bars[i].low
    return all(j == i or bars[j].low > lo for j in range(i - w, i + w + 1))


def _is_pivot_high(bars: Sequence[MarketBar], i: int, w: int) -> bool:
    if i - w < 0 or i + w >= len(bars):
        return False
    hi = bars[i].high
    return all(j == i or bars[j].high < hi for j in range(i - w, i + w + 1))


def _recent_lows(bars: Sequence[MarketBar], index: int, lookback: int = 40) -> list[float]:
    start = max(0, index - lookback)
    return sorted({b.low for b in bars[start:index] if b.low < bars[index].close}, reverse=True)


def _recent_highs(bars: Sequence[MarketBar], index: int, lookback: int = 40) -> list[float]:
    start = max(0, index - lookback)
    return sorted({b.high for b in bars[start:index] if b.high > bars[index].close})


def _nearest_below(levels: Sequence[float], price: float) -> float | None:
    below = [level for level in levels if level < price]
    return max(below) if below else None


def _nearest_above(levels: Sequence[float], price: float) -> float | None:
    above = [level for level in levels if level > price]
    return min(above) if above else None


def _nearest_level(levels: Sequence[MarketLevel], price: float, side: Side) -> float | None:
    if side is Side.LONG:
        above = [level.price for level in levels if level.price > price]
        return min(above) if above else None
    below = [level.price for level in levels if level.price < price]
    return max(below) if below else None


def _shadow_row(setup: IFVGSetup, timeframe_minutes: int) -> dict[str, Any]:
    c = setup.candidate
    plan = setup.plan
    ts = datetime.now(timezone.utc).isoformat()
    return {
        "recorded_at": ts,
        "timeframe_minutes": timeframe_minutes,
        "side": c.side.value,
        "score": setup.score,
        "grade": setup.grade,
        "verdict": setup.verdict,
        "zone_bot": f"{c.gap_bot:.5f}",
        "zone_top": f"{c.gap_top:.5f}",
        "entry": f"{plan.entry:.5f}",
        "stop": f"{plan.stop:.5f}",
        "tp1": f"{plan.tp1:.5f}",
        "tp2": f"{plan.tp2:.5f}",
        "tp3": f"{plan.tp3:.5f}",
        "checklist": json.dumps([item.__dict__ for item in setup.checklist], separators=(",", ":")),
        "warnings": "|".join(setup.warnings),
        "external_bias": setup.external_research.bias if setup.external_research else "",
        "external_confidence": setup.external_research.confidence if setup.external_research else "",
        "external_news_risk": setup.external_research.news_risk if setup.external_research else "",
        "external_supports_trade": setup.external_research.supports_trade if setup.external_research else "",
        "external_should_block": setup.external_research.should_block_trade if setup.external_research else "",
        "external_warnings_json": json.dumps(setup.external_research.warnings if setup.external_research else [], separators=(",", ":")),
        "external_summary": setup.external_research.summary if setup.external_research else "",
        "external_sources_json": json.dumps(setup.external_research.sources if setup.external_research else [], separators=(",", ":")),
        "outcome_r": "",
        "outcome_note": "",
    }
