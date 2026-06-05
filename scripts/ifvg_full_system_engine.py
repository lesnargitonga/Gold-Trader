#!/usr/bin/env python3
"""Full-system IFVG decision engine for Gold Trader.

IFVG remains the only execution trigger. Everything else is a confirmation,
blocker, risk guard, or operator-notification layer: all timeframes, live
market health, session/volatility/spread, macro calendar, sentiment, journal
guards, and alert outputs. Defaults to paper/alert decisions; live execution
requires explicit external wiring.
"""
from __future__ import annotations

import csv, json, math, os, sys, time, urllib.error, urllib.parse, urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

Side = Literal["buy", "sell", "none"]
Bias = Literal["bullish", "bearish", "mixed", "unknown"]

@dataclass
class Candle:
    time: str; open: float; high: float; low: float; close: float; volume: float = 0.0

@dataclass
class TimeframeRead:
    timeframe: str
    candles: int
    current_price: float | None
    bias: Bias
    ifvg_side: Side
    ifvg_zone_low: float | None = None
    ifvg_zone_high: float | None = None
    displacement: bool = False
    liquidity_sweep: bool = False
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

@dataclass
class DailyGuard:
    date: str
    trades_taken: int = 0
    losses_taken: int = 0
    open_positions: int = 0
    blocked: bool = False
    reasons: list[str] = field(default_factory=list)

@dataclass
class MarketContext:
    spread_points: float | None = None
    spread_source: str = "unknown"
    session: str = "unknown"
    volatility_state: str = "unknown"
    macro_state: str = "unknown"
    sentiment_score: float | None = None
    sentiment_state: str = "unknown"
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sentiment_penalty: int = 0
    volatility_penalty: int = 0

@dataclass
class Decision:
    timestamp_utc: str
    symbol: str
    action: str
    side: Side
    final_grade: str
    final_score: int
    current_price: float | None
    entry_low: float | None
    entry_high: float | None
    stop_loss: float | None
    tp1: float | None
    tp2: float | None
    tp3: float | None
    rr_tp1: float | None
    rr_tp2: float | None
    timeframe_reads: list[TimeframeRead]
    daily_guard: DailyGuard
    market_context: MarketContext
    reasons: list[str]
    blockers: list[str]
    next_update: str
    operator_message: str

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text()) if path.exists() else default
    except Exception:
        return default


def bridge_secret_candidates() -> list[str]:
    candidates: list[str] = []

    def add(value: str) -> None:
        secret = (value or "").strip()
        if secret and secret not in candidates:
            candidates.append(secret)

    env = os.getenv("GOLD_BRIDGE_SECRET", "").strip()
    add(env)
    data = load_json(REPO / "config" / "secrets.json", {})
    if isinstance(data, dict):
        add(str(data.get("bridge_secret") or ""))
    cred_path = Path.home() / ".gold-mt5-wine" / "credentials.env"
    try:
        if cred_path.exists():
            for raw in cred_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == "GOLD_BRIDGE_SECRET":
                    add(value.strip().strip("'\""))
    except Exception:
        pass
    return candidates

def load_policy() -> dict[str, Any]:
    default = {
        "symbol": os.getenv("GOLD_SYMBOL", "XAUUSD"),
        "max_trades_per_day": 3,
        "max_open_positions": 1,
        "stop_after_daily_losses": 2,
        "minimum_final_score": 82,
        "minimum_rr_to_tp1": 1.0,
        "minimum_rr_to_tp2": 1.8,
        "timeframes": {
            "higher_timeframe_bias": ["D1", "H4", "H1"],
            "confirmation": ["M30", "M15"],
            "entry_timing": ["M5", "M1"],
            "all": ["D1", "H4", "H1", "M30", "M15", "M5", "M1"],
        },
        "confirmation_rules": {
            "minimum_aligned_timeframes": 5,
            "block_if_d1_and_h4_conflict": True,
        },
        "market_filters": {
            "max_spread_points": 55,
            "allowed_sessions_utc": ["london", "new_york", "london_new_york_overlap"],
            "block_high_impact_macro_minutes_before": 45,
            "block_high_impact_macro_minutes_after": 30,
            "minimum_sentiment_abs_score": 0.15,
            "block_if_sentiment_conflicts": True,
            "block_if_context_unavailable": False,
            "require_live_spread_for_trade": True,
            "require_fresh_macro_for_live": True,
            "require_fresh_sentiment_for_live": True,
            "sentiment_additional_penalty": 12,
            "block_on_dead_volatility": True,
            "volatility_penalty_points": 12
        },
        "journal": {
            "paths": ["logs/trade_journal.csv", "data/paper/trades.csv", "data/live/trades.csv"]
        },
        "operator_updates": {
            "json_path": "logs/ifvg_mtf_decision_state.json",
            "markdown_path": "logs/ifvg_mtf_operator_brief.md",
            "alerts_jsonl_path": "logs/operator_alerts.jsonl"
        },
    }
    configured = load_json(REPO / "config" / "execution_policy.json", {})
    merged = default | configured
    merged["timeframes"] = default["timeframes"] | configured.get("timeframes", {})
    merged["confirmation_rules"] = default["confirmation_rules"] | configured.get("confirmation_rules", {})
    merged["market_filters"] = default["market_filters"] | configured.get("market_filters", {})
    merged["journal"] = default["journal"] | configured.get("journal", {})
    merged["operator_updates"] = default["operator_updates"] | configured.get("operator_updates", {})
    return merged

def bridge_request(path: str, params: dict[str, Any]) -> Any:
    bridge_url = os.getenv("GOLD_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
    url = f"{bridge_url}{path}?{urllib.parse.urlencode(params)}"
    last_error: Exception | None = None
    for secret in [*bridge_secret_candidates(), ""]:
        req = urllib.request.Request(url)
        if secret:
            req.add_header("X-GOLD-BRIDGE-SECRET", secret)
            req.add_header("X-Gold-Bridge-Secret", secret)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in {401, 403}:
                continue
            raise
        except Exception as exc:
            last_error = exc
            break
    if last_error:
        raise last_error
    raise RuntimeError("bridge request failed")

def fetch_candles(symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
    def _tf_to_minutes(tf: str | int) -> int:
        try:
            if isinstance(tf, int):
                return int(tf)
            s = str(tf).upper().strip()
            if s.startswith("M") and s[1:].isdigit():
                return int(s[1:])
            if s.startswith("H") and s[1:].isdigit():
                return int(s[1:]) * 60
            if s.startswith("D") and s[1:].isdigit():
                return int(s[1:]) * 1440
            return int(s)
        except Exception:
            return 15

    tf_minutes = _tf_to_minutes(timeframe)
    rows = bridge_request("/candles", {"symbol": symbol, "timeframe": tf_minutes, "limit": limit})
    return [Candle(str(r.get("time") or r.get("timestamp") or ""), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r.get("volume", 0) or 0)) for r in rows][-limit:]

def read_csv_fallback(symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
    paths = [REPO / "data" / "agent_live_xauusd" / f"{symbol.lower()}_{timeframe.lower()}.csv", REPO / "data" / "agent_live_xauusd" / f"xauusd_{timeframe.lower()}.csv"]
    for path in paths:
        if not path.exists():
            continue
        out = []
        with path.open("r", newline="") as f:
            for r in csv.DictReader(f):
                out.append(Candle(str(r.get("time") or r.get("timestamp") or r.get("datetime") or ""), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r.get("volume", 0) or 0)))
        return out[-limit:]
    return []

def fetch_twelvedata(symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
    try:
        from gold_trader.data.twelvedata import fetch_twelvedata_candles, twelvedata_configured
    except ImportError:
        return []
    if not twelvedata_configured():
        return []
    rows = fetch_twelvedata_candles(symbol, timeframe, limit=limit, repo=REPO)
    return [
        Candle(
            str(r["time"]),
            float(r["open"]),
            float(r["high"]),
            float(r["low"]),
            float(r["close"]),
            float(r.get("volume", 0) or 0),
        )
        for r in rows
    ]


def load_candles(symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
    try:
        candles = fetch_candles(symbol, timeframe, limit)
        if candles:
            return candles
    except Exception:
        pass
    candles = fetch_twelvedata(symbol, timeframe, limit)
    if candles:
        return candles
    return read_csv_fallback(symbol, timeframe, limit)

def atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1: return 0.0
    vals = []
    for prev, cur in zip(candles, candles[1:]):
        vals.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    recent = vals[-period:]
    return sum(recent) / len(recent) if recent else 0.0

def ma(candles: list[Candle], period: int) -> float | None:
    return None if len(candles) < period else sum(c.close for c in candles[-period:]) / period

def infer_bias(candles: list[Candle]) -> tuple[Bias, list[str], int]:
    if len(candles) < 60: return "unknown", ["not enough candles"], 0
    m20, m50, cur = ma(candles, 20), ma(candles, 50), candles[-1]
    score, reasons = 0, []
    if m20 is None or m50 is None: return "unknown", ["MA unavailable"], 0
    if cur.close > m20 > m50:
        score += 35; reasons.append("price above 20/50 MA stack")
    elif cur.close < m20 < m50:
        score -= 35; reasons.append("price below 20/50 MA stack")
    recent_high, recent_low = max(c.high for c in candles[-20:-1]), min(c.low for c in candles[-20:-1])
    if cur.close > recent_high: score += 20; reasons.append("closed above recent structure high")
    if cur.close < recent_low: score -= 20; reasons.append("closed below recent structure low")
    if score >= 25: return "bullish", reasons, abs(score)
    if score <= -25: return "bearish", reasons, abs(score)
    return "mixed", reasons or ["no clean trend stack"], abs(score)

def detect_ifvg(candles: list[Candle]) -> tuple[Side, float | None, float | None, list[str]]:
    if len(candles) < 80: return "none", None, None, ["not enough candles for IFVG"]
    cur, candidates = candles[-1], []
    for i in range(max(2, len(candles)-80), len(candles)-3):
        c0, c2 = candles[i-2], candles[i]
        if c0.high < c2.low: candidates.append(("bullish_fvg", i, c0.high, c2.low))
        if c0.low > c2.high: candidates.append(("bearish_fvg", i, c2.high, c0.low))
    for kind, i, zl, zh in reversed(candidates):
        after = candles[i+1:]
        if kind == "bullish_fvg" and any(c.close < zl for c in after) and cur.high >= zl and (cur.close < zl or cur.close < cur.open):
            return "sell", float(zl), float(zh), ["bullish FVG inverted bearish and retested"]
        if kind == "bearish_fvg" and any(c.close > zh for c in after) and cur.low <= zh and (cur.close > zh or cur.close > cur.open):
            return "buy", float(zl), float(zh), ["bearish FVG inverted bullish and retested"]
    return "none", None, None, ["no confirmed IFVG retest"]

def detect_displacement(candles: list[Candle]) -> bool:
    if len(candles) < 20: return False
    a, c = atr(candles), candles[-1]
    return a > 0 and abs(c.close - c.open) >= a * 0.65

def detect_liquidity_sweep(candles: list[Candle]) -> bool:
    if len(candles) < 30: return False
    cur, prev = candles[-1], candles[-21:-1]
    hi, lo = max(c.high for c in prev), min(c.low for c in prev)
    return (cur.high > hi and cur.close < hi) or (cur.low < lo and cur.close > lo)

def analyze_timeframe(symbol: str, timeframe: str) -> TimeframeRead:
    candles = load_candles(symbol, timeframe)
    if not candles:
        return TimeframeRead(timeframe, 0, None, "unknown", "none", warnings=["no live/cached candle data"])
    bias, breasons, bscore = infer_bias(candles)
    side, zl, zh, ireasons = detect_ifvg(candles)
    disp, sweep = detect_displacement(candles), detect_liquidity_sweep(candles)
    score = max(0, min(100, bscore + (25 if side != "none" else 0) + (10 if disp else 0) + (10 if sweep else 0)))
    return TimeframeRead(timeframe, len(candles), candles[-1].close, bias, side, zl, zh, disp, sweep, score, breasons + ireasons)


def utc_hour() -> int:
    return datetime.now(timezone.utc).hour

def trading_session() -> str:
    h = utc_hour()
    if 12 <= h < 16:
        return "london_new_york_overlap"
    if 7 <= h < 12:
        return "london"
    if 16 <= h < 21:
        return "new_york"
    if 0 <= h < 6:
        return "asia"
    return "off_peak"

def latest_tick(symbol: str) -> dict[str, Any]:
    for path in ("/tick", "/price", "/quote"):
        try:
            data = bridge_request(path, {"symbol": symbol})
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}

def read_spread_points(symbol: str) -> tuple[float | None, str | None]:
    # Prefer normalized market context health file written by updater
    norm = REPO / "logs" / "market_context_health.json"
    if norm.exists():
        data = load_json(norm, {})
        sp = data.get("spread_points")
        if sp is not None:
            source = str(data.get("spread_source") or "").strip().lower()
            if source == "live_tick":
                return float(sp), "live_tick" if data.get("fresh") else "stale_live_tick"
            src = "normalized market_context_health.json"
            if data.get("fresh"):
                return float(sp), f"live normalized {src}"
            return float(sp), f"stale normalized {src}"

    # Fall back to live bridge tick if available
    tick = latest_tick(symbol)
    bid = tick.get("bid"); ask = tick.get("ask")
    if bid is not None and ask is not None:
        point = float(tick.get("point") or 0.01)
        return max(0.0, (float(ask) - float(bid)) / point), "live_tick"
    for key in ("spread_points", "spread", "spread_float"):
        if key in tick:
            return float(tick[key]), f"live tick {key}"

    # Legacy cached market_health.json fallback
    cache = REPO / "logs" / "market_health.json"
    data = load_json(cache, {})
    if data.get("spread_points") is not None:
        spread_src = str(data.get("spread_source") or "").lower()
        if spread_src == "live_tick" or spread_src == "mt5_bridge_last_tick":
            return float(data["spread_points"]), "live_tick"
        if "live" in spread_src:
            return float(data["spread_points"]), "live cached market_health.json"
        return float(data["spread_points"]), "cached market_health.json"
    return None, None

def volatility_state(symbol: str, reads: list[TimeframeRead]) -> tuple[str, list[str]]:
    m15 = next((r for r in reads if r.timeframe == "M15" and r.candles > 0), None)
    candles = load_candles(symbol, "M15", 120) if m15 else []
    if len(candles) < 40:
        return "unknown", ["M15 volatility unavailable"]
    current_atr = atr(candles, 14)
    ranges = [c.high - c.low for c in candles[-60:]]
    avg_range = sum(ranges) / len(ranges) if ranges else 0.0
    if avg_range <= 0 or current_atr <= 0:
        return "unknown", ["ATR/range unavailable"]
    ratio = current_atr / avg_range
    if ratio > 1.7:
        return "extreme", ["M15 ATR is extreme versus recent range"]
    if ratio < 0.45:
        return "dead", ["M15 ATR is too compressed"]
    return "normal", ["M15 volatility is tradable"]

def parse_event_time(value: str) -> datetime | None:
    if not value:
        return None
    value = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def load_macro_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    json_paths = [REPO / "data" / "macro" / "economic_calendar.json", REPO / "logs" / "economic_calendar.json"]
    for path in json_paths:
        data = load_json(path, None)
        if isinstance(data, list):
            events.extend(x for x in data if isinstance(x, dict))
        elif isinstance(data, dict) and isinstance(data.get("events"), list):
            events.extend(x for x in data["events"] if isinstance(x, dict))
    csv_paths = [REPO / "data" / "macro" / "economic_calendar.csv", REPO / "logs" / "economic_calendar.csv"]
    for path in csv_paths:
        if path.exists():
            with path.open("r", newline="") as f:
                events.extend(dict(r) for r in csv.DictReader(f))
    return events

def macro_filter(policy: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    before = int(policy["market_filters"].get("block_high_impact_macro_minutes_before", 45))
    after = int(policy["market_filters"].get("block_high_impact_macro_minutes_after", 30))
    now = datetime.now(timezone.utc)
    # Prefer explicit macro state files that include top-level metadata: {state, updated_at, source}
    state_candidates = [REPO / "data" / "macro" / "economic_calendar.json", REPO / "logs" / "economic_calendar.json"]
    max_age = int(policy["market_filters"].get("macro_state_max_age_minutes", 180))
    for path in state_candidates:
        data = load_json(path, None)
        if isinstance(data, dict) and data.get("state"):
            updated = parse_event_time(str(data.get("updated_at") or ""))
            if not updated and path.exists():
                try:
                    updated = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                except Exception:
                    updated = None
            age_mins = (now - updated).total_seconds() / 60.0 if updated else None
            if age_mins is None or age_mins > max_age:
                return "unknown", ["macro calendar stale or missing updated_at"], [f"{path.relative_to(REPO)} stale"]
            return str(data.get("state") or "unknown"), [], [f"macro state loaded from {path.relative_to(REPO)}"]

    events = load_macro_events()
    if not events:
        return "unknown", [], ["economic calendar unavailable; add data/macro/economic_calendar.json or CSV"]
    blockers, notes = [], []
    for ev in events:
        impact = str(ev.get("impact") or ev.get("importance") or "").lower()
        currency = str(ev.get("currency") or ev.get("country") or "").upper()
        name = str(ev.get("name") or ev.get("event") or ev.get("title") or "macro event")
        dt = parse_event_time(str(ev.get("time_utc") or ev.get("datetime") or ev.get("time") or ""))
        if not dt or "high" not in impact or (currency and currency not in {"USD", "US", "XAU", "GOLD"}):
            continue
        mins = (dt - now).total_seconds() / 60.0
        if -after <= mins <= before:
            blockers.append(f"high-impact {currency or 'USD'} macro window: {name} at {dt.isoformat()}")
        elif abs(mins) <= 180:
            notes.append(f"nearby high-impact event: {name} at {dt.isoformat()}")
    return ("blocked" if blockers else "clear"), blockers, notes

def load_sentiment_state(policy: dict[str, Any]) -> tuple[float | None, str, list[str]]:
    candidates = [REPO / "logs" / "sentiment_state.json", REPO / "data" / "sentiment" / "news_sentiment.json", REPO / "logs" / "news_sentiment.json"]
    notes: list[str] = []
    max_age = int(policy.get("market_filters", {}).get("sentiment_state_max_age_minutes", 60))
    now = datetime.now(timezone.utc)
    for path in candidates:
        data = load_json(path, None)
        if not isinstance(data, dict):
            continue
        # If file uses explicit state metadata
        if data.get("state") is not None:
            updated = parse_event_time(str(data.get("updated_at") or ""))
            if not updated and path.exists():
                try:
                    updated = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                except Exception:
                    updated = None
            age = (now - updated).total_seconds() / 60.0 if updated else None
            if age is None or age > max_age:
                notes.append(f"sentiment file {path.relative_to(REPO)} stale or missing updated_at")
                continue
            # Prefer explicit score when available
            for key in ("gold_score", "xauusd_score", "score", "sentiment_score"):
                if data.get(key) is not None:
                    score = max(-1.0, min(1.0, float(data[key])))
                    state = "bullish" if score > 0.15 else "bearish" if score < -0.15 else "neutral"
                    notes.append(f"sentiment loaded from {path.relative_to(REPO)}")
                    return score, state, notes
            # If no explicit numeric score, map state string
            st = str(data.get("state") or "unknown").lower()
            state = "bullish" if "bull" in st else "bearish" if "bear" in st else "neutral"
            notes.append(f"sentiment state loaded from {path.relative_to(REPO)}")
            return None, state, notes
        # Fallback: older-style JSON with score fields
        for key in ("gold_score", "xauusd_score", "score", "sentiment_score"):
            if data.get(key) is not None:
                score = max(-1.0, min(1.0, float(data[key])))
                state = "bullish" if score > 0.15 else "bearish" if score < -0.15 else "neutral"
                notes.append(f"sentiment loaded from {path.relative_to(REPO)}")
                return score, state, notes
    return None, "unknown", ["sentiment unavailable; add logs/sentiment_state.json with score -1..1"]

def build_market_context(symbol: str, policy: dict[str, Any], reads: list[TimeframeRead], side: Side) -> MarketContext:
    ctx = MarketContext(session=trading_session())
    mf = policy["market_filters"]
    allowed = set(mf.get("allowed_sessions_utc", []))
    if allowed and ctx.session not in allowed:
        ctx.blockers.append(f"session {ctx.session} not in allowed sessions {sorted(allowed)}")
    spread, spread_src = read_spread_points(symbol)
    ctx.spread_points = spread
    ctx.spread_source = spread_src or "unknown"
    # Prefer live tick spread for trade-readiness; cached spread allows paper analysis only
    if spread is None:
        msg = "spread unavailable from bridge/cache"
        (ctx.blockers if mf.get("block_if_context_unavailable") else ctx.warnings).append(msg)
    else:
        # Exceeds max spread
        if spread > float(mf.get("max_spread_points", 55)):
            ctx.blockers.append(f"spread too wide: {spread:.1f} points")
        else:
            ctx.notes.append(f"spread ok: {spread:.1f} points via {spread_src}")
        # If spread is present but not sourced from a live tick, optionally block live trade-readiness
        if spread_src is None or not str(spread_src).lower().startswith("live"):
            if mf.get("require_live_spread_for_trade", True):
                ctx.blockers.append("live tick spread unavailable; live trade not allowed")
            else:
                ctx.warnings.append(f"spread only available via {spread_src}; live tick preferred")
    ctx.volatility_state, vol_notes = volatility_state(symbol, reads)
    ctx.notes.extend(vol_notes)
    if ctx.volatility_state in {"extreme", "dead"}:
        # Policy controls whether dead/extreme volatility blocks live or applies a penalty
        if mf.get("block_on_dead_volatility", True):
            ctx.blockers.append(f"volatility state is {ctx.volatility_state}")
        else:
            ctx.volatility_penalty = int(mf.get("volatility_penalty_points", 12))
            ctx.warnings.append(f"volatility state is {ctx.volatility_state}; penalty applied")
    ctx.macro_state, macro_blockers, macro_notes = macro_filter(policy)
    ctx.blockers.extend(macro_blockers); ctx.notes.extend(macro_notes)
    if ctx.macro_state == "unknown" and mf.get("block_if_context_unavailable"):
        ctx.blockers.append("macro calendar unavailable")
    # Enforce fresh normalized macro file for live trade authority
    macro_norm = REPO / "data" / "macro" / "economic_calendar.json"
    if macro_norm.exists():
        md = load_json(macro_norm, {})
        # If normalized macro explicitly reports blocked and is fresh, treat as a live blocker
        md_state = str(md.get("state") or "").lower()
        if md_state == "blocked" and md.get("fresh", False) and mf.get("require_fresh_macro_for_live", True):
            ctx.blockers.append("macro state blocked; live trade not allowed")
        if not md.get("fresh", False) and mf.get("require_fresh_macro_for_live", True):
            ctx.blockers.append("macro state stale/unavailable; live trade not allowed")
        elif not md.get("fresh", False):
            ctx.warnings.append("macro state stale/unavailable; paper only")
    score, sent_state, sent_notes = load_sentiment_state(policy)
    ctx.sentiment_score = score; ctx.sentiment_state = sent_state; ctx.notes.extend(sent_notes)
    if score is None and mf.get("block_if_context_unavailable"):
        ctx.blockers.append("sentiment unavailable")
        # apply penalty when sentiment is unavailable
        ctx.sentiment_penalty = int(mf.get("sentiment_additional_penalty", 12))
    # Enforce fresh normalized sentiment file for live trade authority
    sent_norm = REPO / "logs" / "sentiment_state.json"
    if sent_norm.exists():
        sd = load_json(sent_norm, {})
        if not sd.get("fresh", False) and mf.get("require_fresh_sentiment_for_live", True):
            ctx.blockers.append("sentiment state stale/unavailable; live trade not allowed")
            ctx.sentiment_penalty = int(mf.get("sentiment_additional_penalty", 12))
        elif not sd.get("fresh", False):
            ctx.warnings.append("sentiment state stale/unavailable; paper only")
    if mf.get("block_if_sentiment_conflicts") and side in {"buy", "sell"} and sent_state in {"bullish", "bearish"}:
        if side == "buy" and sent_state == "bearish":
            ctx.blockers.append("sentiment conflicts with buy side")
        if side == "sell" and sent_state == "bullish":
            ctx.blockers.append("sentiment conflicts with sell side")
    return ctx

def load_journal_counts(policy: dict[str, Any]) -> tuple[int, int, int, list[str]]:
    today = datetime.now(timezone.utc).date().isoformat()
    trades = losses = open_pos = 0
    notes: list[str] = []
    for rel in policy.get("journal", {}).get("paths", []):
        path = REPO / rel
        if not path.exists():
            continue
        with path.open("r", newline="") as f:
            for r in csv.DictReader(f):
                t = str(r.get("date") or r.get("opened_at") or r.get("time") or r.get("timestamp") or "")[:10]
                if t != today:
                    continue
                trades += 1
                pnl = r.get("pnl") or r.get("profit") or r.get("net")
                status = str(r.get("status") or r.get("state") or "").lower()
                if status in {"open", "active"}:
                    open_pos += 1
                try:
                    if pnl is not None and float(pnl) < 0:
                        losses += 1
                except Exception:
                    pass
        notes.append(f"journal counted {path.relative_to(REPO)}")
    return trades, losses, open_pos, notes

def fetch_open_positions(symbol: str) -> int | None:
    for path in ("/positions", "/open_positions"):
        try:
            data = bridge_request(path, {"symbol": symbol})
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict):
                if isinstance(data.get("positions"), list):
                    return len(data["positions"])
                if data.get("count") is not None:
                    return int(data["count"])
        except Exception:
            continue
    return None

def send_operator_alert(policy: dict[str, Any], decision: dict[str, Any]) -> None:
    alert_path = REPO / policy["operator_updates"].get("alerts_jsonl_path", "logs/operator_alerts.jsonl")
    alert_path.parent.mkdir(parents=True, exist_ok=True)
    alert_path.open("a").write(json.dumps({"ts": utc_now(), "decision": decision}, default=str) + "\n")
    text = decision.get("operator_message", "")[:3500]
    webhook = os.getenv("GOLD_ALERT_WEBHOOK_URL", "").strip()
    if webhook:
        try:
            data = json.dumps({"text": text}).encode("utf-8")
            req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", "").strip(), os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and chat:
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode("utf-8")
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=5).read()
        except Exception:
            pass

def load_daily_guard(policy: dict[str, Any]) -> DailyGuard:
    today = datetime.now(timezone.utc).date().isoformat()
    data = load_json(REPO / "data" / "paper" / "daily_guard.json", {})
    guard = DailyGuard(today) if data.get("date") != today else DailyGuard(today, int(data.get("trades_taken",0) or 0), int(data.get("losses_taken",0) or 0), int(data.get("open_positions",0) or 0))
    jt, jl, jo, notes = load_journal_counts(policy)
    if jt or jl or jo:
        guard.trades_taken = max(guard.trades_taken, jt)
        guard.losses_taken = max(guard.losses_taken, jl)
        guard.open_positions = max(guard.open_positions, jo)
        guard.reasons.extend(notes)
    live_open = fetch_open_positions(str(policy.get("symbol", "XAUUSD")))
    if live_open is not None:
        guard.open_positions = max(guard.open_positions, live_open)
        guard.reasons.append(f"open positions from live bridge: {live_open}")
    if guard.trades_taken >= int(policy["max_trades_per_day"]): guard.blocked = True; guard.reasons.append("daily trade limit reached")
    if guard.losses_taken >= int(policy["stop_after_daily_losses"]): guard.blocked = True; guard.reasons.append("daily loss stop reached")
    if guard.open_positions >= int(policy["max_open_positions"]): guard.blocked = True; guard.reasons.append("maximum open positions reached")
    return guard

def side_from_bias(bias: Bias) -> Side:
    return "buy" if bias == "bullish" else "sell" if bias == "bearish" else "none"

def grade(score: int) -> str:
    return "A+" if score >= 90 else "A" if score >= 82 else "B" if score >= 70 else "C" if score >= 55 else "D"

def reward_risk(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    return 0.0 if risk <= 0 else abs(target-entry)/risk

def geometry(side: Side, reads: list[TimeframeRead]):
    cands = [r for r in reads if r.ifvg_side == side and r.ifvg_zone_low is not None and r.ifvg_zone_high is not None]
    if side == "none" or not cands: return (None,)*8
    priority = {"M1":0,"M5":1,"M15":2,"M30":3,"H1":4,"H4":5,"D1":6}
    r = sorted(cands, key=lambda x: priority.get(x.timeframe,99))[0]
    el, eh = float(r.ifvg_zone_low), float(r.ifvg_zone_high)
    entry, zone = (el+eh)/2, abs(eh-el)
    buf = max(zone*0.35, 0.8)
    if side == "buy":
        stop = el - buf; risk = entry - stop; tp1,tp2,tp3 = entry+risk, entry+2*risk, entry+3*risk
    else:
        stop = eh + buf; risk = stop - entry; tp1,tp2,tp3 = entry-risk, entry-2*risk, entry-3*risk
    return el, eh, stop, tp1, tp2, tp3, reward_risk(entry,stop,tp1), reward_risk(entry,stop,tp2)

def fmt(x):
    return "n/a" if x is None or (isinstance(x,float) and math.isnan(x)) else f"{x:.2f}"

def render(action, side, final_grade, score, price, el, eh, stop, tp1, tp2, tp3, rr1, rr2, reasons, blockers, next_update):
    out = ["# Gold Trader IFVG MTF Brief", "", f"Action: {action}", f"Side: {side}", f"Grade: {final_grade} · Score: {score}", f"Current: {fmt(price)}", f"Entry zone: {fmt(el)} – {fmt(eh)}", f"Stop: {fmt(stop)}", f"TP1/TP2/TP3: {fmt(tp1)} / {fmt(tp2)} / {fmt(tp3)}", f"RR TP1/TP2: {fmt(rr1)} / {fmt(rr2)}", "", "## Why"]
    out += [f"- {r}" for r in reasons]
    if blockers:
        out += ["", "## Blockers"] + [f"- {b}" for b in blockers]
    out += ["", "## What to do now", next_update]
    return "\n".join(out) + "\n"

def decide(policy, reads, guard):
    usable = [r for r in reads if r.candles > 0]
    price = usable[-1].current_price if usable else None
    blockers = list(guard.reasons) if guard.blocked else []
    htf_names, entry_names = set(policy["timeframes"]["higher_timeframe_bias"]), set(policy["timeframes"]["entry_timing"])
    htf = [r for r in usable if r.timeframe in htf_names]
    buys = sum(1 for r in usable if side_from_bias(r.bias)=="buy" or r.ifvg_side=="buy")
    sells = sum(1 for r in usable if side_from_bias(r.bias)=="sell" or r.ifvg_side=="sell")
    hbuys = sum(1 for r in htf if side_from_bias(r.bias)=="buy")
    hsells = sum(1 for r in htf if side_from_bias(r.bias)=="sell")
    side: Side = "buy" if hbuys > hsells and buys > sells else "sell" if hsells > hbuys and sells > buys else "none"
    if side == "none": blockers.append("higher-timeframe and all-timeframe votes are not aligned")
    market_context = build_market_context(str(policy["symbol"]), policy, usable, side)
    blockers.extend(market_context.blockers)
    d1, h4 = next((r for r in usable if r.timeframe=="D1"), None), next((r for r in usable if r.timeframe=="H4"), None)
    if d1 and h4 and side_from_bias(d1.bias) != "none" and side_from_bias(h4.bias) != "none" and side_from_bias(d1.bias) != side_from_bias(h4.bias): blockers.append("D1 and H4 conflict")
    # Higher-timeframe alignment requirement: at least 2/3 of HTFs (and minimum 2) must align
    htf_total = len(htf)
    if htf_total:
        htf_aligned = sum(1 for r in htf if side != "none" and (side_from_bias(r.bias) == side or r.ifvg_side == side))
        required_htf = max(2, (2 * htf_total + 2) // 3)  # ceil(2/3 * htf_total) with minimum 2
        if htf_aligned < required_htf:
            blockers.append(f"only {htf_aligned}/{htf_total} higher-timeframes align; need at least {required_htf}")
    aligned = sum(1 for r in usable if side != "none" and (side_from_bias(r.bias)==side or r.ifvg_side==side))
    min_aligned = int(policy["confirmation_rules"].get("minimum_aligned_timeframes", 5))
    if aligned < min_aligned: blockers.append(f"only {aligned}/{len(usable)} timeframes align; need at least {min_aligned}")
    if side != "none":
        # Confirmation requirement flags can be controlled via policy.confirmation_rules
        req_ifvg = bool(policy.get("confirmation_rules", {}).get("require_ifvg_confirm", True))
        req_entry_ifvg = bool(policy.get("confirmation_rules", {}).get("require_entry_ifvg", True))
        req_aligned_sweep = bool(policy.get("confirmation_rules", {}).get("require_aligned_sweep", True))

        if req_ifvg and not any(r.ifvg_side == side for r in usable):
            blockers.append("no IFVG confirms selected side")
        if req_entry_ifvg and not any(r.ifvg_side == side and r.timeframe in entry_names for r in usable):
            blockers.append("entry timeframe does not confirm IFVG")
        if req_aligned_sweep and not any((r.displacement or r.liquidity_sweep) and (r.ifvg_side == side or side_from_bias(r.bias)==side) for r in usable):
            blockers.append("no aligned liquidity sweep or displacement")
    el, eh, stop, tp1, tp2, tp3, rr1, rr2 = geometry(side, usable)
    if rr1 is not None and rr1 < float(policy["minimum_rr_to_tp1"]): blockers.append("TP1 reward/risk below policy")
    if rr2 is not None and rr2 < float(policy["minimum_rr_to_tp2"]): blockers.append("TP2 reward/risk below policy")
    # Base score calculation; subtract blockers and any explicit penalties from market context
    base = 35 + aligned * 7
    base += (10 if any(r.ifvg_side == side for r in usable) else 0)
    base += (8 if any(r.timeframe in entry_names and r.ifvg_side == side for r in usable) else 0)
    base += (8 if any(r.displacement for r in usable) else 0)
    base += (8 if any(r.liquidity_sweep for r in usable) else 0)
    penalty_from_blockers = 12 * len(blockers)
    extra_penalty = int(market_context.sentiment_penalty or 0) + int(market_context.volatility_penalty or 0)
    score = max(0, min(100, base - penalty_from_blockers - extra_penalty))
    if score < int(policy["minimum_final_score"]): blockers.append("final score below Grade-A policy")
    final_grade = grade(score)
    action = "WAIT" if blockers else "TRADE_READY_PAPER_AUTO_ALERT_AUTO"
    reasons = [f"{aligned}/{len(usable)} timeframes align {side}"] if action != "WAIT" else ["setup is not clean enough for Grade-A execution"]
    reasons.extend(market_context.notes)
    if market_context.warnings:
        reasons.extend([f"warning: {w}" for w in market_context.warnings])
    next_update = "Paper/alert allowed by policy; live remains off unless explicitly enabled." if action != "WAIT" else "Wait for all-timeframe alignment, clean IFVG retest, acceptable spread/session/volatility, clear macro window, and non-conflicting sentiment."
    msg = render(action, side, final_grade, score, price, el, eh, stop, tp1, tp2, tp3, rr1, rr2, reasons, blockers, next_update)
    return Decision(utc_now(), str(policy["symbol"]), action, side, final_grade, score, price, el, eh, stop, tp1, tp2, tp3, rr1, rr2, reads, guard, market_context, reasons, blockers, next_update, msg)

def main() -> int:
    policy = load_policy(); symbol = str(policy["symbol"])
    reads = [analyze_timeframe(symbol, tf) for tf in policy["timeframes"]["all"]]
    decision = decide(policy, reads, load_daily_guard(policy))
    jp = REPO / policy["operator_updates"]["json_path"]; mp = REPO / policy["operator_updates"]["markdown_path"]
    jp.parent.mkdir(parents=True, exist_ok=True); mp.parent.mkdir(parents=True, exist_ok=True)
    decision_dict = asdict(decision)
    # Normalize canonical trading symbol at the source. Do NOT let broker/instrument
    # alias leak into the trading decision identity. Store broker alias separately.
    try:
        broker_sym = str(decision_dict.get("symbol") or "").strip()
    except Exception:
        broker_sym = ""
    # Canonical trading symbol used throughout the system and published to Render
    canonical_symbol = "XAUUSD"
    decision_dict["broker_symbol"] = broker_sym if broker_sym else None
    decision_dict["symbol"] = canonical_symbol
    decision_dict["symbol_display"] = "XAU/USD"

    spread_source = str((decision_dict.get("market_context") or {}).get("spread_source") or "unknown")
    decision_dict["data_health"] = {"spread": spread_source}
    jp.write_text(json.dumps(decision_dict, indent=2, allow_nan=False)); mp.write_text(decision.operator_message)
    send_operator_alert(policy, decision_dict)
    print(json.dumps(decision_dict, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
