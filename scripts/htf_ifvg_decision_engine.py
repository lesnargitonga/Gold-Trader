#!/usr/bin/env python3
"""Higher-timeframe IFVG decision engine for Gold Trader.

IFVG-only, all-timeframe confirmation, max 3 trades/day, operator brief output.
Defaults to paper/alert decisions; this file does not place live trades.
"""
from __future__ import annotations

import csv, json, math, os, sys, urllib.parse, urllib.request
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
    current_price: float
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
        "operator_updates": {
            "json_path": "logs/ifvg_mtf_decision_state.json",
            "markdown_path": "logs/ifvg_mtf_operator_brief.md",
        },
    }
    configured = load_json(REPO / "config" / "execution_policy.json", {})
    merged = default | configured
    merged["timeframes"] = default["timeframes"] | configured.get("timeframes", {})
    merged["confirmation_rules"] = default["confirmation_rules"] | configured.get("confirmation_rules", {})
    merged["operator_updates"] = default["operator_updates"] | configured.get("operator_updates", {})
    return merged

def bridge_request(path: str, params: dict[str, Any]) -> Any:
    bridge_url = os.getenv("GOLD_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
    url = f"{bridge_url}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url)
    secret = os.getenv("GOLD_BRIDGE_SECRET", "").strip()
    if secret:
        req.add_header("X-GOLD-BRIDGE-SECRET", secret)
        req.add_header("X-Gold-Bridge-Secret", secret)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

def fetch_candles(symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
    rows = bridge_request("/candles", {"symbol": symbol, "timeframe": timeframe, "limit": limit})
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

def load_candles(symbol: str, timeframe: str, limit: int = 500) -> list[Candle]:
    try:
        return fetch_candles(symbol, timeframe, limit)
    except Exception:
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
        return TimeframeRead(timeframe, 0, math.nan, "unknown", "none", warnings=["no live/cached candle data"])
    bias, breasons, bscore = infer_bias(candles)
    side, zl, zh, ireasons = detect_ifvg(candles)
    disp, sweep = detect_displacement(candles), detect_liquidity_sweep(candles)
    score = max(0, min(100, bscore + (25 if side != "none" else 0) + (10 if disp else 0) + (10 if sweep else 0)))
    return TimeframeRead(timeframe, len(candles), candles[-1].close, bias, side, zl, zh, disp, sweep, score, breasons + ireasons)

def load_daily_guard(policy: dict[str, Any]) -> DailyGuard:
    today = datetime.now(timezone.utc).date().isoformat()
    data = load_json(REPO / "data" / "paper" / "daily_guard.json", {})
    guard = DailyGuard(today) if data.get("date") != today else DailyGuard(today, int(data.get("trades_taken",0) or 0), int(data.get("losses_taken",0) or 0), int(data.get("open_positions",0) or 0))
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
    d1, h4 = next((r for r in usable if r.timeframe=="D1"), None), next((r for r in usable if r.timeframe=="H4"), None)
    if d1 and h4 and side_from_bias(d1.bias) != "none" and side_from_bias(h4.bias) != "none" and side_from_bias(d1.bias) != side_from_bias(h4.bias): blockers.append("D1 and H4 conflict")
    aligned = sum(1 for r in usable if side != "none" and (side_from_bias(r.bias)==side or r.ifvg_side==side))
    min_aligned = int(policy["confirmation_rules"].get("minimum_aligned_timeframes", 5))
    if aligned < min_aligned: blockers.append(f"only {aligned}/{len(usable)} timeframes align; need at least {min_aligned}")
    if side != "none":
        if not any(r.ifvg_side == side for r in usable): blockers.append("no IFVG confirms selected side")
        if not any(r.ifvg_side == side and r.timeframe in entry_names for r in usable): blockers.append("entry timeframe does not confirm IFVG")
        if not any((r.displacement or r.liquidity_sweep) and (r.ifvg_side == side or side_from_bias(r.bias)==side) for r in usable): blockers.append("no aligned liquidity sweep or displacement")
    el, eh, stop, tp1, tp2, tp3, rr1, rr2 = geometry(side, usable)
    if rr1 is not None and rr1 < float(policy["minimum_rr_to_tp1"]): blockers.append("TP1 reward/risk below policy")
    if rr2 is not None and rr2 < float(policy["minimum_rr_to_tp2"]): blockers.append("TP2 reward/risk below policy")
    score = max(0, min(100, 35 + aligned*7 + (10 if any(r.ifvg_side==side for r in usable) else 0) + (8 if any(r.timeframe in entry_names and r.ifvg_side==side for r in usable) else 0) + (8 if any(r.displacement for r in usable) else 0) + (8 if any(r.liquidity_sweep for r in usable) else 0) - 12*len(blockers)))
    if score < int(policy["minimum_final_score"]): blockers.append("final score below Grade-A policy")
    final_grade = grade(score)
    action = "WAIT" if blockers else "TRADE_READY_PAPER_AUTO_ALERT_AUTO"
    reasons = [f"{aligned}/{len(usable)} timeframes align {side}"] if action != "WAIT" else ["setup is not clean enough for Grade-A execution"]
    next_update = "Paper/alert allowed by policy; live remains off unless explicitly enabled." if action != "WAIT" else "Wait for D1/H4/H1 alignment plus M15/M5 IFVG retest with sweep/displacement."
    msg = render(action, side, final_grade, score, price, el, eh, stop, tp1, tp2, tp3, rr1, rr2, reasons, blockers, next_update)
    return Decision(utc_now(), str(policy["symbol"]), action, side, final_grade, score, price, el, eh, stop, tp1, tp2, tp3, rr1, rr2, reads, guard, reasons, blockers, next_update, msg)

def main() -> int:
    policy = load_policy(); symbol = str(policy["symbol"])
    reads = [analyze_timeframe(symbol, tf) for tf in policy["timeframes"]["all"]]
    decision = decide(policy, reads, load_daily_guard(policy))
    jp = REPO / policy["operator_updates"]["json_path"]; mp = REPO / policy["operator_updates"]["markdown_path"]
    jp.parent.mkdir(parents=True, exist_ok=True); mp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_text(json.dumps(asdict(decision), indent=2)); mp.write_text(decision.operator_message)
    print(json.dumps(asdict(decision), indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
