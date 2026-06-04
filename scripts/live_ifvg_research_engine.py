#!/usr/bin/env python3
"""
Live IFVG Research Engine for XAU/USD.

Purpose:
- Pull candles from MT5 bridge or local CSV.
- Detect inversion FVG setups.
- Build entry plan.
- Pull market levels.
- Ask OpenAI for external research: DXY, yields, gold news, CME/options context.
- Output a manual-approval trade plan.

Important:
- This does NOT place trades.
- This does NOT auto-enable live trading.
- This is a grading and decision-support engine only.
"""

from __future__ import annotations

import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


Side = Literal["buy", "sell"]
Bias = Literal["bullish_gold", "bearish_gold", "mixed", "unknown"]


# ----------------------------
# Data models
# ----------------------------


@dataclass
class Candle:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class IFVGSetup:
    side: Side
    timeframe: str
    detected_at: str
    zone_low: float
    zone_high: float
    entry_low: float
    entry_high: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    risk_points: float
    rr_tp1: float
    rr_tp2: float
    rr_tp3: float
    setup_score: int
    setup_grade: str
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ExternalResearch:
    enabled: bool
    mode: str
    bias: Bias = "unknown"
    supports_trade: bool = False
    should_block_trade: bool = False
    confidence: int = 0
    news_risk: str = "unknown"
    macro: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    summary: str = ""
    sources: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FinalDecision:
    timestamp_utc: str
    symbol: str
    current_price: float
    setup: IFVGSetup | None
    external_research: ExternalResearch
    manual_approval_required: bool
    action: str
    final_grade: str
    final_score: int
    warnings: list[str]


# ----------------------------
# Config helpers
# ----------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: str | Path, default: Any) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def load_market_levels(path: str = "config/market_levels.json") -> list[float]:
    data = load_json(path, default={})
    if isinstance(data, list):
        return [float(x) for x in data]
    if isinstance(data, dict):
        rows = data.get("levels", [])
        # rows may be list of dicts with 'price' keys or a list of numbers
        out: list[float] = []
        for item in rows:
            if isinstance(item, dict) and "price" in item:
                try:
                    out.append(float(item["price"]))
                except Exception:
                    continue
            else:
                try:
                    out.append(float(item))
                except Exception:
                    continue
        return out
    return []


# ----------------------------
# Candle loading
# ----------------------------


def load_candles_from_csv(path: str | Path, limit: int = 500) -> list[Candle]:
    """
    Expected CSV columns:
    time/open/high/low/close/volume
    or timestamp/open/high/low/close/volume
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    rows: list[Candle] = []
    with p.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            t = r.get("time") or r.get("timestamp") or r.get("datetime") or ""
            rows.append(
                Candle(
                    time=str(t),
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r.get("volume", 0) or 0),
                )
            )
    return rows[-limit:]


def fetch_candles_from_mt5_bridge(
    *,
    bridge_url: str,
    symbol: str = "XAUUSD",
    timeframe: str = "M15",
    limit: int = 500,
    secret: str | None = None,
) -> list[Candle]:
    """
    Adjust endpoint path if your bridge uses a different route.

    Expected JSON response:
    [
      {"time": "...", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 123}
    ]
    """
    query = urllib.parse.urlencode(
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "limit": limit,
        }
    )
    url = f"{bridge_url.rstrip('/')}/candles?{query}"

    req = urllib.request.Request(url)
    if secret:
        req.add_header("X-GOLD-BRIDGE-SECRET", secret)

    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    candles = []
    for r in payload:
        candles.append(
            Candle(
                time=str(r.get("time") or r.get("timestamp")),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r.get("volume", 0) or 0),
            )
        )
    return candles[-limit:]


# ----------------------------
# Technical helpers
# ----------------------------


def atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0

    trs = []
    for i in range(1, len(candles)):
        prev = candles[i - 1]
        c = candles[i]
        tr = max(
            c.high - c.low,
            abs(c.high - prev.close),
            abs(c.low - prev.close),
        )
        trs.append(tr)

    recent = trs[-period:]
    return sum(recent) / len(recent) if recent else 0.0


def nearest_levels(price: float, levels: list[float], side: Side) -> list[float]:
    if side == "buy":
        return sorted([x for x in levels if x > price], key=lambda x: abs(x - price))
    return sorted([x for x in levels if x < price], key=lambda x: abs(x - price))


def rr(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        return 0.0
    return reward / risk


def detect_simple_ifvg(
    candles: list[Candle],
    *,
    timeframe: str,
    market_levels: list[float],
) -> IFVGSetup | None:
    """
    Conservative starter IFVG detector.

    Logic:
    - Detect recent 3-candle FVG.
    - Detect inversion: close through the gap in the opposite direction.
    - Detect retest/rejection near the inverted zone.
    - Build trade plan.

    Mapping:
    - Bullish FVG inverted downward = sell.
    - Bearish FVG inverted upward = buy.
    """

    if len(candles) < 80:
        return None

    current = candles[-1]
    a = atr(candles, 14)
    if a <= 0:
        return None

    candidates: list[dict[str, Any]] = []

    # Find recent FVGs in last 60 candles.
    for i in range(max(2, len(candles) - 60), len(candles) - 3):
        c0 = candles[i - 2]
        c1 = candles[i - 1]
        c2 = candles[i]

        # Bullish FVG: c0.high < c2.low.
        if c0.high < c2.low:
            candidates.append(
                {
                    "type": "bullish_fvg",
                    "formed_index": i,
                    "zone_low": c0.high,
                    "zone_high": c2.low,
                }
            )

        # Bearish FVG: c0.low > c2.high.
        if c0.low > c2.high:
            candidates.append(
                {
                    "type": "bearish_fvg",
                    "formed_index": i,
                    "zone_low": c2.high,
                    "zone_high": c0.low,
                }
            )

    if not candidates:
        return None

    # Work newest first.
    for fvg in reversed(candidates):
        zone_low = float(fvg["zone_low"])
        zone_high = float(fvg["zone_high"])
        formed_index = int(fvg["formed_index"])
        after = candles[formed_index + 1 :]

        if len(after) < 5:
            continue

        # Bullish FVG inverted bearish: close below zone low, then retest zone.
        if fvg["type"] == "bullish_fvg":
            inverted = any(c.close < zone_low for c in after)
            if not inverted:
                continue

            # Retest: current candle trades into/near zone and rejects lower.
            touched_zone = current.high >= zone_low
            rejection = current.close < zone_low or current.close < current.open

            if touched_zone and rejection:
                side: Side = "sell"
                entry_low = zone_low
                entry_high = zone_high
                buffer = max(a * 0.10, 0.5)
                stop = zone_high + buffer
                entry = min(max(current.close, entry_low), entry_high)
                risk_points = abs(stop - entry)

                if risk_points <= 0:
                    continue

                tp1 = entry - risk_points
                tp2 = entry - 2 * risk_points

                below_levels = nearest_levels(entry, market_levels, side)
                tp3 = below_levels[0] if below_levels else entry - 3 * risk_points

                score, grade, warnings = score_ifvg_setup(
                    side=side,
                    candles=candles,
                    zone_low=zone_low,
                    zone_high=zone_high,
                    entry=entry,
                    stop=stop,
                    tp1=tp1,
                    tp2=tp2,
                    market_levels=market_levels,
                )

                return IFVGSetup(
                    side=side,
                    timeframe=timeframe,
                    detected_at=current.time,
                    zone_low=zone_low,
                    zone_high=zone_high,
                    entry_low=entry_low,
                    entry_high=entry_high,
                    stop_loss=stop,
                    tp1=tp1,
                    tp2=tp2,
                    tp3=tp3,
                    risk_points=risk_points,
                    rr_tp1=rr(entry, stop, tp1),
                    rr_tp2=rr(entry, stop, tp2),
                    rr_tp3=rr(entry, stop, tp3),
                    setup_score=score,
                    setup_grade=grade,
                    warnings=warnings,
                    notes=["Bullish FVG inverted bearish: sell retest."],
                )

        # Bearish FVG inverted bullish: close above zone high, then retest zone.
        if fvg["type"] == "bearish_fvg":
            inverted = any(c.close > zone_high for c in after)
            if not inverted:
                continue

            touched_zone = current.low <= zone_high
            rejection = current.close > zone_high or current.close > current.open

            if touched_zone and rejection:
                side = "buy"
                entry_low = zone_low
                entry_high = zone_high
                buffer = max(a * 0.10, 0.5)
                stop = zone_low - buffer
                entry = min(max(current.close, entry_low), entry_high)
                risk_points = abs(entry - stop)

                if risk_points <= 0:
                    continue

                tp1 = entry + risk_points
                tp2 = entry + 2 * risk_points

                above_levels = nearest_levels(entry, market_levels, side)
                tp3 = above_levels[0] if above_levels else entry + 3 * risk_points

                score, grade, warnings = score_ifvg_setup(
                    side=side,
                    candles=candles,
                    zone_low=zone_low,
                    zone_high=zone_high,
                    entry=entry,
                    stop=stop,
                    tp1=tp1,
                    tp2=tp2,
                    market_levels=market_levels,
                )

                return IFVGSetup(
                    side=side,
                    timeframe=timeframe,
                    detected_at=current.time,
                    zone_low=zone_low,
                    zone_high=zone_high,
                    entry_low=entry_low,
                    entry_high=entry_high,
                    stop_loss=stop,
                    tp1=tp1,
                    tp2=tp2,
                    tp3=tp3,
                    risk_points=risk_points,
                    rr_tp1=rr(entry, stop, tp1),
                    rr_tp2=rr(entry, stop, tp2),
                    rr_tp3=rr(entry, stop, tp3),
                    setup_score=score,
                    setup_grade=grade,
                    warnings=warnings,
                    notes=["Bearish FVG inverted bullish: buy retest."],
                )

    return None


def score_ifvg_setup(
    *,
    side: Side,
    candles: list[Candle],
    zone_low: float,
    zone_high: float,
    entry: float,
    stop: float,
    tp1: float,
    tp2: float,
    market_levels: list[float],
) -> tuple[int, str, list[str]]:
    """
    Starter grading:
    - This is technical grading only.
    - External research is added later.
    """
    score = 50
    warnings: list[str] = []

    current = candles[-1]
    a = atr(candles, 14)

    # Reward/risk gate.
    rr1 = rr(entry, stop, tp1)
    rr2 = rr(entry, stop, tp2)

    if rr1 >= 1.0:
        score += 10
    else:
        warnings.append("TP1 is less than 1R.")
        score -= 10

    if rr2 >= 1.8:
        score += 10
    else:
        warnings.append("TP2 is less than 1.8R.")
        score -= 5

    # Rejection quality.
    body = abs(current.close - current.open)
    candle_range = max(current.high - current.low, 0.00001)
    body_pct = body / candle_range

    if body_pct >= 0.45:
        score += 10
    else:
        warnings.append("Rejection candle body is weak.")
        score -= 5

    # Avoid trading directly into nearby market level.
    near = []
    for lvl in market_levels:
        if abs(lvl - entry) <= max(a * 0.5, 2.0):
            near.append(lvl)

    if near:
        score -= 5
        warnings.append(f"Entry is near configured market level(s): {near[:3]}")
    else:
        score += 5

    # Basic extension warning.
    last5 = candles[-5:]
    same_direction = all(c.close < c.open for c in last5) if side == "sell" else all(c.close > c.open for c in last5)
    if same_direction:
        warnings.append("Move may be overextended; avoid late entry.")
        score -= 10
    else:
        score += 5

    score = max(0, min(100, score))

    if score >= 80:
        grade = "A"
    elif score >= 65:
        grade = "B"
    elif score >= 50:
        grade = "C"
    else:
        grade = "D"

    return score, grade, warnings


# ----------------------------
# OpenAI research
# ----------------------------


def openai_research_enabled() -> bool:
    return os.getenv("GOLD_OPENAI_RESEARCH", "off").lower() in {"soft", "hard"}


def run_openai_market_research(
    *,
    symbol: str,
    current_price: float,
    setup: IFVGSetup | None,
    market_levels: list[float],
) -> ExternalResearch:
    mode = os.getenv("GOLD_OPENAI_RESEARCH", "off").lower()
    model = os.getenv("GOLD_OPENAI_RESEARCH_MODEL", "gpt-5.4")

    if mode == "off":
        return ExternalResearch(
            enabled=False,
            mode=mode,
            warnings=["OpenAI research disabled."],
            summary="External research not used.",
        )

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return ExternalResearch(
            enabled=True,
            mode=mode,
            warnings=["OPENAI_API_KEY missing. External OpenAI research unavailable."],
            summary="OpenAI unavailable because API key is missing.",
        )

    if setup is None:
        return ExternalResearch(
            enabled=True,
            mode=mode,
            warnings=["No IFVG setup. OpenAI research not called."],
            summary="No setup to research.",
        )

    try:
        from openai import OpenAI
    except Exception as exc:
        return ExternalResearch(
            enabled=True,
            mode=mode,
            warnings=[f"OpenAI SDK unavailable: {exc}"],
            summary="OpenAI research failed before request.",
        )

    client = OpenAI(api_key=api_key)

    system = """
You are a market-context research assistant for an XAUUSD IFVG trading assistant.
You do not give financial advice.
You do not place trades.
You do not create trade ideas.
You only evaluate whether external context supports, weakens, or warns against an existing IFVG setup.
Return strict JSON only.
Be conservative.
If data is unavailable or uncertain, say unknown.
Do not invent CME/options values.
Do not overstate confidence.
External research is a grading layer, not a trade elimination layer.
In soft mode, never block a trade.
"""

    user = {
        "symbol": symbol,
        "current_price": current_price,
        "setup": asdict(setup),
        "operator_market_levels": market_levels,
        "task": [
            "Research current gold/XAUUSD context.",
            "Check DXY direction.",
            "Check US 10Y yield direction.",
            "Check real yields if available.",
            "Check CME gold futures/options/open interest context if available.",
            "Check Investing.com gold/XAUUSD options context if available.",
            "Check high-impact USD calendar risk.",
            "Return external grading and warnings for this IFVG setup.",
        ],
        "required_json": {
            "bias": "bullish_gold | bearish_gold | mixed | unknown",
            "supports_trade": "boolean",
            "should_block_trade": "boolean",
            "confidence": "0-100",
            "news_risk": "low | medium | high | unknown",
            "macro": {
                "dxy_bias": "supports_buy | supports_sell | neutral | unknown",
                "us10y_bias": "supports_buy | supports_sell | neutral | unknown",
                "real_yield_bias": "supports_buy | supports_sell | neutral | unknown",
            },
            "options": {
                "bias": "bullish_gold | bearish_gold | neutral | unknown",
                "important_levels": [],
                "danger_zones": [],
                "notes": "string",
            },
            "warnings": [],
            "summary": "string",
            "sources": [],
        },
    }

    try:
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user)},
            ],
            # If your OpenAI account supports web search tools, enable this.
            # Some SDK/accounts may use a slightly different tool name.
            tools=[{"type": "web_search_preview"}],
        )

        text = response.output_text.strip()

        # Clean possible markdown wrapping.
        if text.startswith("```"):
            text = text.strip("`")
            text = text.replace("json", "", 1).strip()

        data = json.loads(text)

        should_block = bool(data.get("should_block_trade", False))
        if mode == "soft":
            should_block = False

        return ExternalResearch(
            enabled=True,
            mode=mode,
            bias=data.get("bias", "unknown"),
            supports_trade=bool(data.get("supports_trade", False)),
            should_block_trade=should_block,
            confidence=int(data.get("confidence", 0)),
            news_risk=data.get("news_risk", "unknown"),
            macro=data.get("macro", {}),
            options=data.get("options", {}),
            warnings=list(data.get("warnings", [])),
            summary=str(data.get("summary", "")),
            sources=list(data.get("sources", [])),
            raw=data,
        )

    except Exception as exc:
        return ExternalResearch(
            enabled=True,
            mode=mode,
            warnings=[f"External OpenAI research unavailable: {exc}"],
            summary="OpenAI call failed; continue with technical IFVG plan only.",
        )


# ----------------------------
# Final decision logic
# ----------------------------


def combine_decision(
    *,
    symbol: str,
    current_price: float,
    setup: IFVGSetup | None,
    research: ExternalResearch,
) -> FinalDecision:
    warnings: list[str] = []

    if setup:
        warnings.extend(setup.warnings)
    warnings.extend(research.warnings)

    if setup is None:
        return FinalDecision(
            timestamp_utc=utc_now(),
            symbol=symbol,
            current_price=current_price,
            setup=None,
            external_research=research,
            manual_approval_required=True,
            action="NO_TRADE",
            final_grade="NONE",
            final_score=0,
            warnings=warnings + ["No IFVG setup detected."],
        )

    score = setup.setup_score

    # OpenAI is grading, not creating trades.
    if research.enabled:
        if research.supports_trade:
            score += min(10, max(0, research.confidence // 10))
        elif research.bias in {"mixed", "unknown"}:
            score -= 0
        else:
            score -= 5

        if research.news_risk == "high":
            warnings.append("High news risk. Manual caution required.")
            score -= 10

        if research.should_block_trade:
            warnings.append("External research hard-blocked this setup.")

    score = max(0, min(100, score))

    if score >= 85:
        grade = "A+"
    elif score >= 75:
        grade = "A"
    elif score >= 65:
        grade = "B"
    elif score >= 50:
        grade = "C"
    else:
        grade = "D"

    if research.should_block_trade:
        action = "WAIT_EXTERNAL_BLOCK"
    elif grade in {"A+", "A", "B"}:
        action = "MANUAL_REVIEW_ENTRY_PLAN"
    else:
        action = "WATCH_ONLY"

    return FinalDecision(
        timestamp_utc=utc_now(),
        symbol=symbol,
        current_price=current_price,
        setup=setup,
        external_research=research,
        manual_approval_required=True,
        action=action,
        final_grade=grade,
        final_score=score,
        warnings=warnings,
    )


# ----------------------------
# Main runner
# ----------------------------


def run_engine() -> FinalDecision:
    symbol = os.getenv("GOLD_SYMBOL", "XAUUSD")
    timeframe = os.getenv("GOLD_TIMEFRAME", "M15")
    market_levels = load_market_levels()

    bridge_url = os.getenv("GOLD_BRIDGE_URL", "").strip()
    bridge_secret = os.getenv("GOLD_BRIDGE_SECRET", "").strip() or None
    csv_path = os.getenv("GOLD_CANDLES_CSV", "").strip()

    if bridge_url:
        candles = fetch_candles_from_mt5_bridge(
            bridge_url=bridge_url,
            symbol=symbol,
            timeframe=timeframe,
            limit=500,
            secret=bridge_secret,
        )
    elif csv_path:
        candles = load_candles_from_csv(csv_path, limit=500)
    else:
        raise RuntimeError(
            "Set GOLD_BRIDGE_URL or GOLD_CANDLES_CSV. "
            "Example: export GOLD_CANDLES_CSV=data/agent_live_xauusd/xauusd_15m.csv"
        )

    current_price = candles[-1].close

    # Convert candles -> MarketBar for production IFVG detector
    try:
        from gold_trader.models import MarketBar
        from gold_trader.assistants.ifvg_confluence import (
            find_ifvg_setups,
            setup_to_dict,
            load_market_levels as ifvg_load_market_levels,
        )
        from gold_trader.calendar import NewsCalendar
        from gold_trader.data.macro import load_macro_frame
    except Exception:
        # If production imports are unavailable, fall back to the simple detector.
        setup = detect_simple_ifvg(
            candles,
            timeframe=timeframe,
            market_levels=market_levels,
        )
        research = run_openai_market_research(
            symbol=symbol,
            current_price=current_price,
            setup=setup,
            market_levels=market_levels,
        )
        return combine_decision(
            symbol=symbol,
            current_price=current_price,
            setup=setup,
            research=research,
        )

    # Build MarketBar list
    mbars: list[MarketBar] = []
    from datetime import datetime as _dt
    for c in candles:
        try:
            ts = _dt.fromisoformat(str(c.time))
        except Exception:
            # fallback: ignore timezone
            try:
                ts = _dt.fromisoformat(str(c.time).replace("Z", "+00:00"))
            except Exception:
                continue
        mbars.append(MarketBar(timestamp=ts, open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume, session="csv_fallback"))

    # Load macro/frame and market levels
    macro_frame = None
    try:
        macro_frame = load_macro_frame("data/macro")
        if not getattr(macro_frame, "names", lambda: [])():
            macro_frame = None
    except Exception:
        macro_frame = None

    levels = ifvg_load_market_levels(Path("config") / "market_levels.json")
    try:
        calendar = NewsCalendar.load(Path("data") / "macro" / "news_calendar.csv")
    except Exception:
        calendar = None

    # Call production detector
    setups = find_ifvg_setups(
        mbars,
        macro_frame=macro_frame,
        market_levels=levels,
        news_calendar=calendar,
        openai_config_path=Path("config") / "openai_research.json",
        openai_cache_path=Path("data") / "cache" / "openai_market_research.json",
        force_external_research=False,
    )

    # If none found, optionally fall back to simple detector
    if not setups:
        setup = detect_simple_ifvg(
            candles,
            timeframe=timeframe,
            market_levels=market_levels,
        )
        research = run_openai_market_research(
            symbol=symbol,
            current_price=current_price,
            setup=setup,
            market_levels=market_levels,
        )
        return combine_decision(
            symbol=symbol,
            current_price=current_price,
            setup=setup,
            research=research,
        )

    # Persist pending approvals for each setup and return a list of decisions
    approvals: list[dict[str, Any]] = []
    base_dir = Path("data") / "pending_approvals"
    base_dir.mkdir(parents=True, exist_ok=True)
    index_path = base_dir / "index.json"
    try:
        idx = json.loads(index_path.read_text()) if index_path.exists() else []
    except Exception:
        idx = []

    def _tf_minutes(tf: str | int) -> int:
        try:
            s = str(tf)
            digits = "".join(ch for ch in s if ch.isdigit())
            return int(digits) if digits else 15
        except Exception:
            return 15

    for s in setups:
        sdict = setup_to_dict(s, timeframe_minutes=_tf_minutes(timeframe))
        grading = sdict.get("grading") or {}
        final_score = grading.get("final_score", s.score if hasattr(s, "score") else 0)
        final_grade = grading.get("letter", s.grade if hasattr(s, "grade") else "C")
        external = sdict.get("external_research")
        warnings = sdict.get("warnings", [])
        manual_required = bool(sdict.get("manual_approval_required", True))

        ts = utc_now().replace(":", "-")
        side = sdict.get("trade_action") or ("buy" if getattr(s.candidate, "side", None) and getattr(s.candidate.side, "value", "") == "long" else "sell")
        fname = f"{ts}_{symbol}_{side}.json"
        fpath = base_dir / fname

        payload = {
            "timestamp_utc": utc_now(),
            "symbol": symbol,
            "setup": sdict,
            "external_research": external,
            "final_score": final_score,
            "final_grade": final_grade,
            "warnings": warnings,
            "manual_approval_required": manual_required,
        }
        try:
            fpath.write_text(json.dumps(payload, indent=2))
        except Exception:
            continue

        idx.append({
            "filename": str(fpath),
            "timestamp_utc": payload["timestamp_utc"],
            "symbol": symbol,
            "side": side,
            "final_score": final_score,
            "final_grade": final_grade,
        })
        approvals.append(payload)

    try:
        index_path.write_text(json.dumps(idx, indent=2))
    except Exception:
        pass

    # Return a consolidated single decision object representing the top setup if needed
    # but for now return the first approval-like decision as FinalDecision-compatible dict
    if approvals:
        top = approvals[0]
        return combine_decision(
            symbol=symbol,
            current_price=current_price,
            setup=None,
            research=ExternalResearch(enabled=False, mode="off" ,summary="see pending approvals"),
        )
    return combine_decision(
        symbol=symbol,
        current_price=current_price,
        setup=None,
        research=ExternalResearch(enabled=False, mode="off", summary="no setups"),
    )


def main() -> None:
    decision = run_engine()
    print(json.dumps(asdict(decision), indent=2))


if __name__ == "__main__":
    main()
