from __future__ import annotations

import csv
import io
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_PKG_ROOT = Path(__file__).resolve().parents[3]
ROOT = Path(
    os.getenv(
        "GOLD_TRADER_ROOT",
        os.getenv("GOLD_REPO_ROOT", os.getenv("GOLD_RUNTIME_ROOT", str(_PKG_ROOT))),
    )
).resolve()
LOGS = ROOT / "logs"
DATA = ROOT / "data"


def _decision_paths() -> list[Path]:
    paths: list[Path] = []
    for base in (
        ROOT,
        _PKG_ROOT,
        Path.cwd(),
        Path(os.getenv("RENDER_PROJECT_DIR", "")) if os.getenv("RENDER_PROJECT_DIR") else None,
        Path("/opt/render/project/src"),
    ):
        if not base:
            continue
        try:
            base = Path(base).resolve()
        except Exception:
            continue
        for rel in (("logs", "ifvg_mtf_decision_state.json"), ("data", "state.json")):
            candidate = base.joinpath(*rel)
            if candidate not in paths:
                paths.append(candidate)
    return paths or [LOGS / "ifvg_mtf_decision_state.json"]

TIMEFRAMES = ["D1", "H4", "H1", "M30", "M15", "M5", "M1"]
HTF = {"D1", "H4", "H1"}
ENTRY_TF = {"M15", "M5", "M1"}

TWELVE_INTERVALS = {
    "M1": "1min",
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
    "D1": "1day",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().replace(microsecond=0).isoformat()


def read_json(path: Path, default: Any = None) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, allow_nan=False, default=str), encoding="utf-8")
    tmp.replace(path)


def parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=timezone.utc)

    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def api_key(name: str) -> str:
    return os.getenv(name, "").strip()


def twelve_symbol() -> str:
    raw = (
        os.getenv("GOLD_TWELVE_DATA_SYMBOL")
        or os.getenv("GOLD_SYMBOL")
        or "XAU/USD"
    ).strip()
    upper = raw.upper().replace("_", "/")
    if upper in {"XAUUSD", "GOLD"}:
        return "XAU/USD"
    return raw


def http_get_text(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "GoldTrader/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_twelve_candles(tf: str, count: int = 300) -> list[dict[str, Any]]:
    key = api_key("TWELVE_DATA_API_KEY")
    interval = TWELVE_INTERVALS.get(tf.upper())
    if not key or not interval:
        return []

    params = {
        "symbol": twelve_symbol(),
        "interval": interval,
        "outputsize": str(count),
        "format": "CSV",
        "apikey": key,
    }
    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(params)
    try:
        raw = http_get_text(url)
    except Exception:
        return []
    if raw.lstrip().startswith("{"):
        return []

    candles: list[dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(raw)):
        try:
            candles.append(
                {
                    "time": row.get("datetime") or row.get("time") or row.get("date") or "",
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume") or 0.0),
                }
            )
        except Exception:
            continue

    candles.reverse()
    return candles


def fetch_twelve_quote() -> dict[str, Any]:
    key = api_key("TWELVE_DATA_API_KEY")
    if not key:
        return {"state": "missing_key", "source": "twelvedata", "updated_at": iso_now()}

    params = {"symbol": twelve_symbol(), "apikey": key}
    url = "https://api.twelvedata.com/quote?" + urllib.parse.urlencode(params)
    try:
        payload = json.loads(http_get_text(url))
    except Exception as exc:
        return {"state": "error", "source": "twelvedata", "error": repr(exc), "updated_at": iso_now()}

    if not isinstance(payload, dict) or payload.get("status") == "error" or payload.get("code"):
        return {
            "state": "error",
            "source": "twelvedata",
            "error": payload.get("message") if isinstance(payload, dict) else str(payload),
            "updated_at": iso_now(),
        }

    out: dict[str, Any] = {
        "state": "unknown_nonfatal_in_paper",
        "source": "twelvedata",
        "symbol": payload.get("symbol") or twelve_symbol(),
        "updated_at": iso_now(),
        "raw_available": True,
    }

    for key_name in ("bid", "ask", "close", "previous_close", "percent_change"):
        if key_name in payload:
            out[key_name] = payload[key_name]

    try:
        bid = float(payload.get("bid"))
        ask = float(payload.get("ask"))
        if bid > 0 and ask >= bid:
            out["spread_points"] = ask - bid
            out["state"] = "ok"
    except Exception:
        pass

    return out


def candle_bias(candles: list[dict[str, Any]]) -> str:
    closes = [float(c["close"]) for c in candles if c.get("close") is not None]
    if len(closes) < 55:
        return "unknown"
    ma20 = sum(closes[-20:]) / 20
    ma50 = sum(closes[-50:]) / 50
    price = closes[-1]
    if price > ma20 > ma50:
        return "bullish"
    if price < ma20 < ma50:
        return "bearish"
    return "mixed"


def atr_volatility(candles: list[dict[str, Any]], period: int = 14) -> dict[str, Any]:
    if len(candles) < period + 2:
        return {"state": "unknown", "source": "twelvedata", "reason": "not enough M15 candles", "updated_at": iso_now()}

    trs: list[float] = []
    prev_close = float(candles[-period - 1]["close"])
    for candle in candles[-period:]:
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close

    atr = sum(trs) / len(trs)
    last_close = float(candles[-1]["close"])
    atr_pct = atr / last_close if last_close else 0.0

    if atr_pct < 0.00025:
        state = "compressed"
    elif atr_pct > 0.0035:
        state = "extreme"
    else:
        state = "normal"

    return {
        "state": state,
        "atr": atr,
        "atr_pct": atr_pct,
        "period": period,
        "source": "twelvedata_M15",
        "updated_at": iso_now(),
    }


def fetch_fmp_calendar() -> dict[str, Any]:
    key = api_key("FMP_API_KEY")
    if not key:
        return {"state": "missing_key", "source": "fmp", "events": [], "updated_at": iso_now()}

    start = now_utc().date() - timedelta(days=1)
    end = now_utc().date() + timedelta(days=3)
    params = {"from": str(start), "to": str(end), "apikey": key}
    url = "https://financialmodelingprep.com/api/v3/economic_calendar?" + urllib.parse.urlencode(params)

    try:
        payload = json.loads(http_get_text(url))
    except Exception as exc:
        return {"state": "error", "source": "fmp", "events": [], "error": repr(exc), "updated_at": iso_now()}

    if isinstance(payload, dict):
        return {"state": "error", "source": "fmp", "events": [], "error": payload, "updated_at": iso_now()}
    if not isinstance(payload, list):
        return {"state": "error", "source": "fmp", "events": [], "error": str(type(payload)), "updated_at": iso_now()}

    terms = (
        "cpi", "ppi", "pce", "nonfarm", "payroll", "nfp", "fomc", "fed", "powell",
        "interest rate", "rate decision", "gdp", "retail sales", "ism", "pmi", "jolts",
        "jobless", "unemployment", "treasury", "consumer confidence", "durable",
    )

    events: list[dict[str, Any]] = []
    for event in payload:
        country = str(event.get("country") or event.get("currency") or "").upper()
        name = str(event.get("event") or event.get("name") or event.get("title") or "")
        impact = str(event.get("impact") or event.get("importance") or "").lower()
        text = f"{name} {impact}".lower()
        is_us = "US" in country or "USD" in country or "UNITED STATES" in country
        if is_us and (any(term in text for term in terms) or "high" in impact):
            events.append(event)

    state = "ok" if events else "ok_no_high_impact"
    result = {"state": state, "source": "fmp", "events": events[:80], "updated_at": iso_now()}
    write_json(DATA / "macro" / "economic_calendar.json", result)
    return result


def compute_macro_state(calendar: dict[str, Any]) -> dict[str, Any]:
    if calendar.get("state") in {"missing_key", "error"}:
        return {"state": "unknown", "source": "fmp", "blockers": ["macro feed unavailable"], "updated_at": iso_now()}

    events = calendar.get("events") or []
    if not events:
        return {"state": "clear", "source": "fmp", "blockers": [], "next_event": None, "updated_at": iso_now()}

    before = int(os.getenv("GOLD_MACRO_BLOCK_BEFORE_MINUTES", "45"))
    after = int(os.getenv("GOLD_MACRO_BLOCK_AFTER_MINUTES", "30"))
    now = now_utc()
    blockers: list[dict[str, Any]] = []
    next_event: dict[str, Any] | None = None
    next_time: datetime | None = None

    for event in events:
        event_time = parse_time(event.get("date") or event.get("datetime") or event.get("time"))
        if not event_time:
            continue
        if event_time >= now and (next_time is None or event_time < next_time):
            next_time = event_time
            next_event = event
        if event_time - timedelta(minutes=before) <= now <= event_time + timedelta(minutes=after):
            blockers.append(
                {
                    "event": event.get("event") or event.get("name") or event.get("title"),
                    "time": event_time.isoformat(),
                }
            )

    if blockers:
        return {"state": "blocked", "source": "fmp", "blockers": blockers, "next_event": next_event, "updated_at": iso_now()}
    return {"state": "clear", "source": "fmp", "blockers": [], "next_event": next_event, "updated_at": iso_now()}


def read_sentiment_state() -> dict[str, Any]:
    payload = read_json(LOGS / "sentiment_state.json", {})
    if not isinstance(payload, dict) or not payload:
        return {"state": "unknown", "score": None, "fresh": False, "source": "none", "updated_at": iso_now()}

    stamp = parse_time(payload.get("updated_at") or payload.get("timestamp") or payload.get("timestamp_utc"))
    max_age = int(os.getenv("GOLD_SENTIMENT_MAX_AGE_SECONDS", "3600"))
    age = int((now_utc() - stamp).total_seconds()) if stamp else None
    fresh = age is not None and age <= max_age

    return {
        **payload,
        "state": payload.get("state") or payload.get("label") or "unknown",
        "age_seconds": age,
        "fresh": fresh,
        "source": payload.get("source", "file"),
    }


def load_decision() -> dict[str, Any]:
    existing = [p for p in _decision_paths() if p.exists()]
    for path in sorted(existing, key=lambda p: p.stat().st_mtime, reverse=True):
        payload = read_json(path, {})
        if isinstance(payload, dict) and payload:
            return payload
    return {
        "timestamp_utc": iso_now(),
        "symbol": "XAUUSD",
        "action": "WAIT_HARD_BLOCK",
        "side": "none",
        "final_score": 0,
        "final_grade": "D",
        "timeframe_reads": [],
        "reasons": ["decision state unavailable"],
        "blockers": ["decision engine has not written state yet"],
    }


def repair_timeframe_candles(decision: dict[str, Any]) -> None:
    reads = decision.setdefault("timeframe_reads", [])
    existing = {str(r.get("timeframe", "")).upper(): r for r in reads if isinstance(r, dict)}

    for tf in TIMEFRAMES:
        row = existing.get(tf)
        if row is None:
            row = {
                "timeframe": tf,
                "candles": 0,
                "current_price": None,
                "bias": "unknown",
                "ifvg_side": "none",
                "ifvg_zone_low": None,
                "ifvg_zone_high": None,
                "displacement": False,
                "liquidity_sweep": False,
                "score": 0,
                "reasons": [],
                "warnings": [],
            }
            reads.append(row)

        if int(row.get("candles") or 0) <= 0:
            candles = fetch_twelve_candles(tf, count=300)
            if candles:
                row["candles"] = len(candles)
                row["current_price"] = float(candles[-1]["close"])
                if str(row.get("bias") or "unknown").lower() == "unknown":
                    row["bias"] = candle_bias(candles)
                warnings = [w for w in (row.get("warnings") or []) if "no live/cached candle data" not in str(w)]
                warnings.append("candles repaired from Twelve Data; IFVG not inferred by repair layer")
                row["warnings"] = warnings


def alignment_audit(decision: dict[str, Any]) -> dict[str, Any]:
    side = str(decision.get("side") or "none").lower()
    reads = decision.get("timeframe_reads") or []
    tf_align: dict[str, str] = {}
    aligned: list[str] = []
    htf_count = 0
    entry_ifvg = False
    entry_displacement = False
    liquidity = False
    active_ifvg = 0

    for row in reads:
        tf = str(row.get("timeframe") or "").upper()
        bias = str(row.get("bias") or "unknown").lower()
        ifvg = str(row.get("ifvg_side") or "none").lower()
        if ifvg not in {"none", "", "unknown"}:
            active_ifvg += 1

        direction_bias = "bearish" if side == "sell" else "bullish" if side == "buy" else ""
        is_aligned = side in {"buy", "sell"} and (bias == direction_bias or ifvg == side)
        tf_align[tf] = "aligned" if is_aligned else (bias if bias else "unknown")

        if is_aligned:
            aligned.append(tf)
            if tf in HTF:
                htf_count += 1

        if tf in ENTRY_TF and ifvg == side:
            entry_ifvg = True
        if tf in ENTRY_TF and bool(row.get("displacement")):
            entry_displacement = True
        if bool(row.get("liquidity_sweep")):
            liquidity = True

    return {
        "side": side,
        "tf_align": tf_align,
        "aligned_count": len(aligned),
        "aligned_timeframes": aligned,
        "total_timeframes": len(reads),
        "htf_aligned": htf_count,
        "htf_total": 3,
        "active_ifvg_reads": active_ifvg,
        "entry_ifvg_confirmed": entry_ifvg,
        "entry_displacement_confirmed": entry_displacement,
        "liquidity_confirmed": liquidity,
    }


def harden_scores(decision: dict[str, Any], live_context: dict[str, Any]) -> dict[str, Any]:
    audit = alignment_audit(decision)
    side = audit["side"]

    macro = live_context["macro_state"]
    sentiment = live_context["sentiment_state"]
    spread = live_context["spread_state"]
    volatility = live_context["volatility_state"]

    missing: list[str] = []
    hard_blocks: list[str] = []
    soft_blocks: list[str] = []

    align_score = min(25, round((audit["aligned_count"] / 7) * 25)) if audit["total_timeframes"] else 0
    if audit["aligned_count"] < 5:
        hard_blocks.append(f"only {audit['aligned_count']}/7 timeframes align; required: 5")
    if audit["htf_aligned"] < 2:
        hard_blocks.append(f"only {audit['htf_aligned']}/3 higher timeframes align; required: 2")

    geometry_score = 0
    if audit["entry_ifvg_confirmed"]:
        geometry_score += 9
    else:
        hard_blocks.append("entry timeframe does not confirm IFVG")
    if audit["entry_displacement_confirmed"]:
        geometry_score += 6
    else:
        hard_blocks.append("entry displacement not confirmed")
    if audit["liquidity_confirmed"]:
        geometry_score += 5
    else:
        hard_blocks.append("no aligned liquidity sweep or displacement context")

    macro_state = str(macro.get("state", "unknown")).lower()
    if macro_state in {"clear", "ok", "ok_no_high_impact"}:
        macro_score = 20
    elif macro_state == "blocked":
        macro_score = 0
        hard_blocks.append("macro event block is active")
    else:
        macro_score = 0
        missing.append("Macro calendar missing")

    sentiment_state = str(sentiment.get("state", "unknown")).lower()
    sentiment_score_raw = sentiment.get("score")
    sentiment_fresh = bool(sentiment.get("fresh", False))
    if not sentiment_fresh:
        sentiment_score = 0
        missing.append("Fresh sentiment missing")
    elif sentiment_state in {"unknown", "", "none"}:
        sentiment_score = 0
        missing.append("Sentiment missing")
    else:
        numeric = None
        try:
            numeric = float(sentiment_score_raw)
        except Exception:
            pass
        conflicts_sell = side == "sell" and ("bullish" in sentiment_state or (numeric is not None and numeric > 0.2))
        conflicts_buy = side == "buy" and ("bearish" in sentiment_state or (numeric is not None and numeric < -0.2))
        if conflicts_sell or conflicts_buy:
            sentiment_score = 7
            hard_blocks.append(f"sentiment conflicts with {side} side")
        else:
            sentiment_score = 15

    market = decision.get("market_context") or {}
    session = str(market.get("session") or "unknown").lower()
    allowed = {"london", "london_new_york_overlap", "new_york"}
    session_ok = session in allowed
    if not session_ok:
        soft_blocks.append(f"Current session is {session.replace('_', ' ')}. Allowed: London, London/New York overlap, New York.")

    spread_state = str(spread.get("state", "unknown")).lower()
    if spread_state == "ok":
        session_score = 10 if session_ok else 5
    elif "unknown" in spread_state:
        session_score = 3 if session_ok else 0
        missing.append("Spread feed missing")
    else:
        session_score = 0
        hard_blocks.append("spread feed unhealthy")

    vol_state = str(volatility.get("state", "unknown")).lower()
    if vol_state == "normal":
        vol_score = 10
    elif vol_state in {"compressed", "high"}:
        vol_score = 5
    else:
        vol_score = 0
        missing.append("Volatility state missing")

    raw = align_score + geometry_score + macro_score + sentiment_score + session_score + vol_score
    penalty = 0
    penalty += 10 if any("Macro" in item for item in missing) else 0
    penalty += 8 if any("Sentiment" in item or "Fresh sentiment" in item for item in missing) else 0
    penalty += 7 if any("Spread" in item for item in missing) else 0
    penalty += 5 if any("Volatility" in item for item in missing) else 0

    stamp = parse_time(decision.get("timestamp_utc"))
    age = int((now_utc() - stamp).total_seconds()) if stamp else None
    if age is None or age > int(os.getenv("GOLD_DECISION_STALE_SECONDS", "300")):
        penalty += 10
        hard_blocks.append("decision state is stale")

    final_score = max(0, min(100, raw - penalty))
    grade = "A+" if final_score >= 92 else "A" if final_score >= 82 else "B" if final_score >= 70 else "C" if final_score >= 55 else "D"

    try:
        rr_ok = float(decision.get("rr_tp2") or 0) >= float(os.getenv("GOLD_MIN_RR_TP2", "2.0"))
    except Exception:
        rr_ok = False
    if not rr_ok:
        hard_blocks.append("RR to TP2 below policy minimum")

    watching_for = []
    if audit["aligned_count"] < 5:
        watching_for.append(f"{5 - audit['aligned_count']} more timeframe alignment votes")
    if not audit["entry_ifvg_confirmed"]:
        watching_for.append("entry-timeframe IFVG retest")
    if not audit["entry_displacement_confirmed"]:
        watching_for.append("entry displacement candle")
    if not audit["liquidity_confirmed"]:
        watching_for.append("liquidity sweep confirmation")
    if any("Macro" in item for item in missing):
        watching_for.append("macro feed healthy")
    if any("Spread" in item for item in missing):
        watching_for.append("spread feed healthy")

    return {
        "score_decomposition": {
            "timeframe_alignment": {"score": align_score, "max": 25},
            "ifvg_geometry": {"score": geometry_score, "max": 20},
            "macro_regime": {"score": macro_score, "max": 20},
            "sentiment_gate": {"score": sentiment_score, "max": 15},
            "session_spread": {"score": session_score, "max": 10},
            "volatility": {"score": vol_score, "max": 10},
        },
        "score_raw": raw,
        "data_quality_penalty": penalty,
        "missing_inputs": list(dict.fromkeys(missing)),
        "hard_blocks": list(dict.fromkeys(hard_blocks)),
        "soft_blocks": list(dict.fromkeys(soft_blocks)),
        "watching_for": list(dict.fromkeys(watching_for)),
        "tf_alignment_audit": audit,
        "source_age_seconds": age,
        "source_state": "fresh" if age is not None and age <= 180 else "borderline" if age is not None and age <= 300 else "stale",
        "hardened_score": final_score,
        "hardened_grade": grade,
        "grade_allowed": final_score >= int(os.getenv("GOLD_GRADE_A_MIN_SCORE", "82")) and not hard_blocks,
    }


def build_provider_health(decision: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    reads = decision.get("timeframe_reads") or []
    candle_total = sum(int(row.get("candles") or 0) for row in reads)

    stamp = parse_time(decision.get("timestamp_utc"))
    age = int((now_utc() - stamp).total_seconds()) if stamp else None
    decision_state = "fresh" if age is not None and age <= 180 else "borderline" if age is not None and age <= 300 else "stale"

    health = {
        "updated_at": iso_now(),
        "decision_state": {
            "age_seconds": age,
            "state": decision_state,
            "label": f"updated {age}s" if age is not None else "unknown",
            "severity": "ok" if decision_state == "fresh" else "warning" if decision_state == "borderline" else "critical",
        },
        "twelvedata": {
            "state": "ok" if candle_total > 0 else "unknown",
            "label": "Twelve Data candles",
            "candles_loaded": candle_total,
            "symbol": twelve_symbol(),
        },
        "fmp_macro": {
            "state": context["macro_state"].get("state", "unknown"),
            "label": "FMP macro calendar",
            "next_event": context["macro_state"].get("next_event"),
        },
        "finnhub_sentiment": {
            "state": context["sentiment_state"].get("state", "unknown"),
            "label": "Finnhub/news sentiment",
            "fresh": context["sentiment_state"].get("fresh"),
        },
        "spread": {
            "state": context["spread_state"].get("state", "unknown"),
            "label": "Spread feed",
            "spread_points": context["spread_state"].get("spread_points"),
        },
        "volatility": {
            "state": context["volatility_state"].get("state", "unknown"),
            "label": "M15 ATR volatility",
            "atr": context["volatility_state"].get("atr"),
        },
        "ctrader": {
            "state": "connected" if api_key("CTRADER_ACCESS_TOKEN") else "pending",
            "label": "cTrader broker",
        },
        "cme": {
            "state": "connected" if api_key("CME_API_KEY") else "not_connected",
            "label": "CME futures/options",
        },
        "options": {
            "state": "connected" if api_key("OPTIONS_API_KEY") else "not_connected",
            "label": "Options/IV/skew",
        },
        "orders": {
            "state": "unlocked" if os.getenv("GOLD_ENABLE_LIVE_ORDERS", "false").lower() == "true" else "locked",
            "label": "Live orders",
        },
    }
    write_json(LOGS / "provider_health.json", health)
    return health


def repair_and_harden() -> dict[str, Any]:
    LOGS.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)

    decision = load_decision()
    repair_timeframe_candles(decision)

    calendar = fetch_fmp_calendar()
    context = {
        "updated_at": iso_now(),
        "macro_state": compute_macro_state(calendar),
        "spread_state": fetch_twelve_quote(),
        "volatility_state": atr_volatility(fetch_twelve_candles("M15", count=100)),
        "sentiment_state": read_sentiment_state(),
        "cot_state": {"state": "unknown", "source": "not_connected"},
        "cme_state": {"state": "not_connected", "source": "not_connected"},
        "options_state": {"state": "not_connected", "source": "not_connected"},
    }

    write_json(LOGS / "live_market_context.json", context)
    write_json(LOGS / "spread_state.json", context["spread_state"])
    write_json(LOGS / "volatility_state.json", context["volatility_state"])

    hardened = harden_scores(decision, context)

    original_score = decision.get("final_score")
    original_grade = decision.get("final_grade")
    decision["final_score_raw_before_hardening"] = original_score
    decision["final_grade_raw_before_hardening"] = original_grade
    decision["final_score"] = hardened["hardened_score"]
    decision["final_grade"] = hardened["hardened_grade"]
    decision["score_decomposition"] = hardened["score_decomposition"]
    decision["score_raw"] = hardened["score_raw"]
    decision["data_quality_penalty"] = hardened["data_quality_penalty"]
    decision["missing_inputs"] = hardened["missing_inputs"]
    decision["hard_blocks"] = hardened["hard_blocks"]
    decision["soft_blocks"] = hardened["soft_blocks"]
    decision["watching_for"] = hardened["watching_for"]
    decision["tf_align"] = hardened["tf_alignment_audit"]["tf_align"]
    decision["tf_alignment_audit"] = hardened["tf_alignment_audit"]
    decision["source_age_seconds"] = hardened["source_age_seconds"]
    decision["source_state"] = hardened["source_state"]
    decision["live_market_context"] = context

    blockers = []
    for item in (decision.get("blockers") or []) + hardened["hard_blocks"] + hardened["soft_blocks"]:
        if not item:
            continue
        text = str(item)
        text = text.replace(
            "session off_peak not in allowed sessions ['london', 'london_new_york_overlap', 'new_york']",
            "Current session is off-peak. Allowed: London, London/New York overlap, New York.",
        )
        if text not in blockers:
            blockers.append(text)
    decision["blockers"] = blockers

    reasons = []
    for item in decision.get("reasons") or []:
        if item and str(item) not in reasons:
            reasons.append(str(item))
    for missing in hardened["missing_inputs"]:
        text = f"{missing}. Grade-A readiness is blocked until this feed is healthy."
        if text not in reasons:
            reasons.append(text)
    decision["reasons"] = reasons

    if not hardened["grade_allowed"]:
        decision["action"] = "WAIT_HARD_BLOCK" if hardened["hard_blocks"] else "WAIT"
        decision["next_update"] = "Wait for all-timeframe alignment, clean IFVG retest, acceptable spread/session/volatility, clear macro window, and non-conflicting sentiment."
    elif str(decision.get("action") or "").startswith("TRADE_READY"):
        pass
    else:
        decision["action"] = "TRADE_READY_PAPER_AUTO_ALERT_AUTO"

    health = build_provider_health(decision, context)
    decision["provider_health"] = health
    decision["cloud_status"] = {
        "analysis": "online",
        "data_provider": "twelvedata" if api_key("TWELVE_DATA_API_KEY") else "missing",
        "candles_loaded": health["twelvedata"]["candles_loaded"],
        "orders": health["orders"]["state"],
        "execution_mode": os.getenv("GOLD_EXECUTION_MODE", "paper"),
        "broker": health["ctrader"]["state"],
        "macro": health["fmp_macro"]["state"],
        "sentiment": health["finnhub_sentiment"]["state"],
        "volatility": health["volatility"]["state"],
        "spread": health["spread"].get("spread_points"),
    }

    write_json(LOGS / "ifvg_mtf_decision_state.json", decision)
    write_json(DATA / "state.json", decision)

    snap_name = now_utc().strftime("%Y%m%dT%H%M%SZ") + f"_{decision.get('action')}_{decision.get('final_score')}.json"
    write_json(LOGS / "decision_snapshots" / snap_name, decision)

    market_intelligence = {
        "macro": context["macro_state"].get("state", "unknown"),
        "sentiment": context["sentiment_state"].get("state", "unknown"),
        "spread": context["spread_state"].get("state", "unknown"),
        "volatility": context["volatility_state"].get("state", "unknown"),
        "cme": context["cme_state"].get("state", "not_connected"),
        "options": context["options_state"].get("state", "not_connected"),
        "cot": context["cot_state"].get("state", "unknown"),
        "updated_at": iso_now(),
    }
    write_json(LOGS / "market_intelligence.json", market_intelligence)

    result = {
        "ok": True,
        "action": decision.get("action"),
        "score": decision.get("final_score"),
        "grade": decision.get("final_grade"),
        "missing_inputs": decision.get("missing_inputs"),
        "hard_blocks": decision.get("hard_blocks"),
        "provider_health": health,
    }

    try:
        from gold_trader.core.market_intelligence_ux import harden_decision

        ui_state = harden_decision()
        result["action"] = ui_state.get("action")
        result["score"] = ui_state.get("final_score")
        result["grade"] = ui_state.get("final_grade")
        result["ui_hardened"] = True
    except Exception as exc:
        result["ui_harden_error"] = repr(exc)

    return result


if __name__ == "__main__":
    print(json.dumps(repair_and_harden(), indent=2, allow_nan=False, default=str))
