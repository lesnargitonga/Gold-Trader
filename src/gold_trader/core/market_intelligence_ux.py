from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PKG_ROOT = Path(__file__).resolve().parents[3]
ROOT = Path(
    os.getenv("GOLD_TRADER_ROOT", os.getenv("GOLD_RUNTIME_ROOT", str(_PKG_ROOT)))
).resolve()
LIVE_CONTEXT_PATH = ROOT / "logs" / "live_market_context.json"
PROVIDER_HEALTH_PATH = ROOT / "logs" / "provider_health.json"
ALERTS_PATH = ROOT / "logs" / "operator_alerts.jsonl"
SNAPSHOT_DIR = ROOT / "logs" / "decision_snapshots"


def _decision_candidates() -> list[Path]:
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
        for rel in (
            ("logs", "ifvg_mtf_decision_state.json"),
            ("logs", "decision_state.json"),
            ("data", "ifvg_mtf_decision_state.json"),
            ("data", "state.json"),
        ):
            candidate = base.joinpath(*rel)
            if candidate not in paths:
                paths.append(candidate)
    return paths or [ROOT / "logs" / "ifvg_mtf_decision_state.json"]

TF_ORDER = ["D1", "H4", "H1", "M30", "M15", "M5", "M1"]
HTF = {"D1", "H4", "H1"}
ENTRY_TFS = {"M15", "M5", "M1"}

_ALLOWED_SESSIONS = {"london", "london_new_york_overlap", "new_york"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None


def age_seconds_from_state(state: dict[str, Any]) -> int | None:
    dt = parse_dt(state.get("timestamp_utc") or state.get("updated_at") or state.get("timestamp"))
    if not dt:
        return None
    return max(0, int((now_utc() - dt).total_seconds()))


def source_age_status(age: int | None) -> dict[str, Any]:
    if age is None:
        return {"age_seconds": None, "state": "unknown", "label": "unknown", "severity": "warning"}
    if age < 60:
        return {"age_seconds": age, "state": "fresh", "label": f"updated {age}s", "severity": "ok"}
    if age < 180:
        return {"age_seconds": age, "state": "borderline", "label": f"updated {age}s", "severity": "warning"}
    return {"age_seconds": age, "state": "stale", "label": f"stale {age}s", "severity": "danger"}


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return value


def read_json(path: Path, default: Any = None) -> Any:
    try:
        if path.exists():
            return _json_safe(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        pass
    return {} if default is None else default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False, allow_nan=False))


def find_decision_path() -> Path:
    existing = [p for p in _decision_candidates() if p.exists()]
    if existing:
        return max(existing, key=lambda p: p.stat().st_mtime)
    return ROOT / "logs" / "ifvg_mtf_decision_state.json"


def norm_side(value: Any) -> str:
    side = str(value or "none").strip().lower()
    if side in {"buy", "bullish", "long"}:
        return "buy"
    if side in {"sell", "bearish", "short"}:
        return "sell"
    return "none"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def build_tf_alignment(decision: dict[str, Any]) -> dict[str, Any]:
    side = norm_side(decision.get("side"))
    reads = decision.get("timeframe_reads") or []
    by_tf: dict[str, Any] = {}
    aligned_count = 0
    htf_aligned = 0
    active_ifvg = 0
    entry_confirmed = False
    entry_displacement = False
    liquidity_confirmed = False

    for tf in TF_ORDER:
        read = next((r for r in reads if str(r.get("timeframe", "")).upper() == tf), {})
        bias = norm_side(read.get("bias"))
        ifvg = norm_side(read.get("ifvg_side"))
        candles = int(_num(read.get("candles"), 0))
        displacement = bool(read.get("displacement"))
        sweep = bool(read.get("liquidity_sweep"))
        has_ifvg = ifvg in {"buy", "sell"}
        if has_ifvg:
            active_ifvg += 1

        aligned = False
        if side in {"buy", "sell"}:
            bias_ok = bias == side or (tf in HTF and bias in {side, "none"})
            ifvg_ok = ifvg == side or (tf in HTF and ifvg in {side, "none"})
            aligned = bias_ok and ifvg_ok and candles > 0
        if aligned:
            aligned_count += 1
            if tf in HTF:
                htf_aligned += 1
        if tf in ENTRY_TFS and ifvg == side and candles > 0:
            entry_confirmed = True
        if tf in ENTRY_TFS and displacement:
            entry_displacement = True
        if sweep:
            liquidity_confirmed = True

        data_state = str(read.get("data_state") or "").lower()
        if candles <= 0 and not data_state:
            data_state = "unavailable"
        by_tf[tf] = {
            "bias": read.get("bias") or "unknown",
            "ifvg_side": read.get("ifvg_side") or "none",
            "candles": candles,
            "aligned": aligned,
            "data_state": data_state,
            "score": int(_num(read.get("score"), 0)),
            "displacement": displacement,
            "liquidity_sweep": sweep,
            "warnings": read.get("warnings") or [],
        }

    return {
        "by_tf": by_tf,
        "aligned_count": aligned_count,
        "required_count": 5,
        "htf_aligned": htf_aligned,
        "htf_required": 2,
        "active_ifvg_reads": active_ifvg,
        "entry_confirmed": entry_confirmed,
        "entry_displacement": entry_displacement,
        "liquidity_confirmed": liquidity_confirmed,
    }


def _feed_state(container: dict[str, Any], key: str, fallback: Any = None) -> Any:
    raw = container.get(key)
    if isinstance(raw, dict):
        return raw.get("state") or fallback
    if raw is not None:
        return raw
    return fallback


def normalize_feed_display(value: Any) -> str:
    text = str(value or "unknown").strip().lower()
    mapping = {
        "dead": "compressed",
        "unknown_nonfatal_in_paper": "unavailable (paper)",
        "ok_no_high_impact": "clear (no high-impact events)",
        "missing_key": "not configured",
    }
    return mapping.get(text, str(value or "unknown"))


def _reason_bucket(text: str) -> str:
    low = text.lower()
    if "macro" in low or "economic calendar" in low:
        return "macro"
    if "spread" in low:
        return "spread"
    if "sentiment" in low:
        return "sentiment"
    if "volatility" in low or "atr" in low:
        return "volatility"
    if "candle feed" in low or "timeframe candle" in low:
        return "candles"
    if "grade-a execution" in low or "not clean enough" in low:
        return "setup_quality"
    return " ".join(low.split())


def _dedupe_lines(items: list[Any]) -> list[str]:
    out: list[str] = []
    seen: list[str] = []
    buckets: set[str] = set()
    for item in items:
        text = human_blocker(item if isinstance(item, str) else str((item or {}).get("label", "")))
        bucket = _reason_bucket(text)
        if bucket in buckets:
            continue
        key = " ".join(text.lower().split())
        if not key:
            continue
        if key in seen or any(key in prev or prev in key for prev in seen):
            continue
        buckets.add(bucket)
        seen.append(key)
        out.append(text)
    return out


def missing_inputs(decision: dict[str, Any], live_context: dict[str, Any]) -> list[dict[str, str]]:
    market = decision.get("market_context") or {}
    live = live_context or {}
    missing: list[dict[str, str]] = []

    spread = market.get("spread_points")
    spread_state = _feed_state(live, "spread_state") or _feed_state(market, "spread_state")
    if spread is None and str(spread_state).lower() not in {"ok", "normal", "tight", "unknown_nonfatal_in_paper"}:
        missing.append({"key": "spread", "label": "Spread feed missing", "impact": "session/spread score capped; live orders blocked"})

    macro = _feed_state(live, "macro_state") or _feed_state(market, "macro_state")
    macro_ok = {"ok", "clear", "mixed", "neutral", "ok_no_high_impact", "caution", "near_event"}
    if not macro or str(macro).lower() not in macro_ok:
        missing.append({"key": "macro", "label": "Macro calendar missing", "impact": "macro regime score = 0 and Grade-A blocked"})

    sentiment = _feed_state(live, "sentiment_state") or _feed_state(market, "sentiment_state")
    if not sentiment or str(sentiment).lower() in {"unknown", "unavailable", "missing", "error"}:
        missing.append({"key": "sentiment", "label": "Sentiment feed missing", "impact": "sentiment gate score = 0 and Grade-A blocked"})

    volatility = _feed_state(live, "volatility_state") or _feed_state(market, "volatility_state")
    vol_s = str(volatility).lower()
    if not volatility or vol_s in {"unknown", "unavailable", "missing", "error"}:
        missing.append({"key": "volatility", "label": "Volatility state missing", "impact": "volatility score = 0"})

    zero_candle_tfs = [
        str(r.get("timeframe", "")).upper()
        for r in (decision.get("timeframe_reads") or [])
        if isinstance(r, dict) and int(r.get("candles") or 0) <= 0
    ]
    if zero_candle_tfs:
        missing.append({
            "key": "candles",
            "label": f"Candle feed missing on {', '.join(zero_candle_tfs)}",
            "impact": "timeframe alignment capped until feeds restore",
        })

    age = age_seconds_from_state(decision)
    if age is None or age >= int(os.getenv("GOLD_STALE_DECISION_SECONDS", "300")):
        missing.append({"key": "source_age", "label": "Decision state stale", "impact": "trade readiness blocked until a fresh scan completes"})

    return missing


def score_decomposition(decision: dict[str, Any], live_context: dict[str, Any], alignment: dict[str, Any], missing: list[dict[str, str]]) -> dict[str, Any]:
    side = norm_side(decision.get("side"))
    market = decision.get("market_context") or {}
    live = live_context or {}

    tf_score = min(25, round((alignment["aligned_count"] / max(1, alignment["required_count"])) * 25))
    if alignment["htf_aligned"] < alignment["htf_required"]:
        tf_score = min(tf_score, 12)

    geom = 0
    if alignment["entry_confirmed"]:
        geom += 10
    if alignment["entry_displacement"]:
        geom += 6
    if alignment["liquidity_confirmed"]:
        geom += 4
    geom = min(20, geom)

    macro_raw = _feed_state(live, "macro_state") or _feed_state(market, "macro_state") or "unknown"
    macro_s = str(macro_raw).lower()
    if macro_s in {"clear", "mixed", "neutral", "ok", "ok_no_high_impact"}:
        macro_score = 20
    elif macro_s in {"caution", "near_event"}:
        macro_score = 8
    elif macro_s == "blocked":
        macro_score = 0
    else:
        macro_score = 0

    sentiment_raw = _feed_state(live, "sentiment_state") or _feed_state(market, "sentiment_state") or "unknown"
    sentiment_s = str(sentiment_raw).lower()
    sentiment_score = 0
    if sentiment_s in {"neutral", "mixed", "non_conflicting", "ok"}:
        sentiment_score = 15
    elif side == "buy" and "bull" in sentiment_s:
        sentiment_score = 15
    elif side == "sell" and "bear" in sentiment_s:
        sentiment_score = 15
    elif "mild" in sentiment_s:
        sentiment_score = 7

    session = str(market.get("session") or "unknown").lower()
    session_ok = session in _ALLOWED_SESSIONS
    spread_raw = str(_feed_state(live, "spread_state") or _feed_state(market, "spread_state") or "").lower()
    spread_ok = market.get("spread_points") is not None or spread_raw in {
        "ok", "normal", "tight", "unknown_nonfatal_in_paper",
    }
    session_spread = 0
    if session_ok:
        session_spread += 5
    if spread_ok:
        session_spread += 5

    vol = str(_feed_state(live, "volatility_state") or _feed_state(market, "volatility_state") or "unknown").lower()
    if vol in {"normal", "tradable", "ok"}:
        volatility = 10
    elif vol in {"compressed", "dead", "high"}:
        volatility = 5
    else:
        volatility = 0

    components = {
        "timeframe_alignment": {"score": int(tf_score), "max": 25, "label": "Timeframe Alignment"},
        "ifvg_geometry": {"score": int(geom), "max": 20, "label": "IFVG Geometry"},
        "macro_regime": {"score": int(macro_score), "max": 20, "label": "Macro Regime"},
        "sentiment_gate": {"score": int(sentiment_score), "max": 15, "label": "Sentiment Gate"},
        "session_spread": {"score": int(session_spread), "max": 10, "label": "Session / Spread"},
        "volatility": {"score": int(volatility), "max": 10, "label": "Volatility"},
    }
    total = sum(v["score"] for v in components.values())
    penalty = 0
    for item in missing:
        if item["key"] == "macro": penalty += 20
        elif item["key"] == "sentiment": penalty += 15
        elif item["key"] == "spread": penalty += 5
        elif item["key"] == "volatility": penalty += 10
        elif item["key"] == "source_age": penalty += 20
    capped = max(0, min(100, total))
    return {"components": components, "raw_total": int(total), "total": int(capped), "data_quality_penalty": int(penalty)}


def human_blocker(text: str) -> str:
    s = str(text or "").strip()
    low = s.lower()
    if "session" in low and "not in allowed" in low:
        return "Current session is off-peak. Allowed: London, London/New York overlap, New York."
    if "spread unavailable" in low:
        return "Spread feed is unavailable. Paper analysis may continue, but live orders are blocked."
    if "economic calendar" in low or "macro" in low and "unavailable" in low:
        return "Macro calendar is unavailable. Grade-A readiness is blocked until macro feed is healthy."
    if "sentiment" in low and "unavailable" in low:
        return "Sentiment feed is unavailable. Grade-A readiness is blocked until sentiment feed is healthy."
    if "only" in low and "timeframes align" in low:
        return s.replace("timeframes", "timeframes").replace("need at least", "required:")
    if "volatility" in low and "dead" in low:
        return "M15 volatility is compressed; avoid chasing until range expands."
    if "fresh sentiment missing" in low or ("sentiment" in low and "missing" in low and "macro" not in low):
        return "Sentiment feed is unavailable. Grade-A readiness is blocked until sentiment feed is healthy."
    if "macro calendar missing" in low:
        return "Macro calendar is unavailable. Grade-A readiness is blocked until macro feed is healthy."
    if "spread feed missing" in low:
        return "Spread feed is unavailable. Paper analysis may continue, but live orders are blocked."
    if "volatility state missing" in low:
        return "Volatility state is unavailable. Grade-A readiness is blocked until M15 ATR feed is healthy."
    if "timeframe candle feed missing" in low or "candle feed missing on" in low:
        return s if s.endswith(".") else f"{s}."
    return s


def watching_for(decision: dict[str, Any], alignment: dict[str, Any], missing: list[dict[str, str]]) -> list[str]:
    out: list[str] = []
    need_tf = max(0, alignment["required_count"] - alignment["aligned_count"])
    if need_tf:
        out.append(f"{need_tf} more timeframe alignment vote{'s' if need_tf != 1 else ''}")
    if alignment["htf_aligned"] < alignment["htf_required"]:
        out.append("higher-timeframe bias agreement")
    if not alignment["entry_confirmed"]:
        out.append("entry-timeframe IFVG retest")
    if not alignment["entry_displacement"]:
        out.append("entry displacement candle")
    if not alignment["liquidity_confirmed"]:
        out.append("liquidity sweep confirmation")
    for item in missing:
        if item["key"] == "macro": out.append("macro feed healthy")
        elif item["key"] == "spread": out.append("spread feed healthy")
        elif item["key"] == "sentiment": out.append("non-conflicting sentiment")
        elif item["key"] == "source_age": out.append("fresh full-system scan")
    return out[:8] or ["hard-block cleared", "Grade-A score threshold"]


def provider_health(decision: dict[str, Any], live_context: dict[str, Any]) -> dict[str, Any]:
    cloud = decision.get("cloud_status") or {}
    age = source_age_status(age_seconds_from_state(decision))
    market = decision.get("market_context") or {}
    macro = _feed_state(live_context, "macro_state") or _feed_state(market, "macro_state") or "unknown"
    sentiment = _feed_state(live_context, "sentiment_state") or _feed_state(market, "sentiment_state") or "unknown"
    spread = _feed_state(live_context, "spread_state") or ("ok" if market.get("spread_points") is not None else "unknown")
    reads = decision.get("timeframe_reads") or []
    zero_candle_tfs = [
        str(r.get("timeframe", "")).upper()
        for r in reads
        if isinstance(r, dict) and int(_num(r.get("candles"), 0)) <= 0
    ]
    candle_warnings = [
        str(w)
        for r in reads
        if isinstance(r, dict)
        for w in (r.get("warnings") or [])
        if w
    ]
    candles_loaded = int(_num(cloud.get("candles_loaded"), 0))
    if candles_loaded <= 0 and reads:
        candles_loaded = sum(int(_num(r.get("candles"), 0)) for r in reads if isinstance(r, dict))

    def file_age(path: Path) -> int | None:
        try:
            if path.exists():
                return max(0, int(now_utc().timestamp() - path.stat().st_mtime))
        except OSError:
            return None
        return None

    def feed_object(*keys: str) -> dict[str, Any]:
        for key in keys:
            raw = live_context.get(key)
            if isinstance(raw, dict):
                return raw
        for key in keys:
            raw = market.get(key)
            if isinstance(raw, dict):
                return raw
        return {}

    def feed_age(feed: dict[str, Any], fallback_path: Path | None = None) -> int | None:
        raw_age = feed.get("age_seconds")
        if raw_age is not None:
            try:
                return max(0, int(float(raw_age)))
            except (TypeError, ValueError):
                pass
        dt = parse_dt(feed.get("updated_at") or feed.get("timestamp_utc") or feed.get("time"))
        if dt:
            return max(0, int((now_utc() - dt).total_seconds()))
        return file_age(fallback_path) if fallback_path else None

    def feed_message(feed: dict[str, Any], default: str = "") -> str:
        for key in ("error", "warning", "message", "summary"):
            value = feed.get(key)
            if value:
                return str(value)
        blockers = feed.get("blockers")
        if isinstance(blockers, list) and blockers:
            return "; ".join(str(x) for x in blockers[:3])
        return default

    def status(
        state: Any,
        label: str,
        *,
        source: str = "",
        configured: bool | None = None,
        message: str = "",
        required_env: list[str] | None = None,
        age_seconds: int | None = None,
        latency_ms: int | None = None,
        severity: str | None = None,
    ) -> dict[str, Any]:
        raw_state = state.get("state") if isinstance(state, dict) else state
        text = str(raw_state or "unknown")
        low = text.lower()
        if severity is None:
            if low in {"ok", "fresh", "clear", "available", "normal", "neutral", "mild_bullish", "mild_bearish", "bullish", "bearish", "manual_proxy"}:
                severity = "ok"
            elif low in {"degraded", "borderline", "unknown_nonfatal_in_paper", "pending", "pending_or_configured", "credentials_present_not_validated", "locked"}:
                severity = "warning"
            else:
                severity = "danger" if low in {"stale", "failed", "error", "blocked"} else "warning"
        if configured is False and severity == "ok":
            severity = "warning"
        payload: dict[str, Any] = {"state": text, "label": label, "severity": severity}
        if source:
            payload["source"] = source
        if configured is not None:
            payload["configured"] = configured
        if message:
            payload["message"] = message
        if required_env:
            payload["required_env"] = required_env
        if age_seconds is not None:
            payload["age_seconds"] = age_seconds
        if latency_ms is not None:
            payload["latency_ms"] = latency_ms
        return payload

    td_message = ""
    td_state = "ok"
    if zero_candle_tfs:
        td_state = "degraded"
        td_message = f"No analysis candles on {', '.join(zero_candle_tfs)}."
    if candle_warnings:
        td_state = "degraded"
        td_message = candle_warnings[0]

    macro_file = ROOT / "data" / "macro" / "economic_calendar.json"
    sentiment_file = ROOT / "logs" / "sentiment_state.json"
    spread_file = ROOT / "logs" / "spread_state.json"
    volatility_file = ROOT / "logs" / "volatility_state.json"
    cot_file = ROOT / "data" / "cot" / "gold_cot_state.json"
    cross_market_file = ROOT / "logs" / "cross_market_state.json"
    levels_file = ROOT / "config" / "market_levels.json"
    macro_feed = feed_object("macro_state", "macro")
    sentiment_feed = feed_object("sentiment_state", "sentiment")
    spread_feed = feed_object("spread_state", "spread")
    volatility_feed = feed_object("volatility_state", "volatility")
    cot_feed = feed_object("cot_state", "cot")
    cross_market_feed = feed_object("cross_market_state", "cross_market")
    cross_market_state = live_context.get("cross_market_state") or market.get("cross_market_state") or "unknown"
    if isinstance(cross_market_feed, dict):
        symbols = cross_market_feed.get("symbols")
        if isinstance(symbols, dict) and symbols:
            usable = [
                value for value in symbols.values()
                if isinstance(value, dict) and not value.get("error") and any(value.get(k) is not None for k in ("close", "percent_change", "change"))
            ]
            if not usable:
                cross_market_state = "unavailable"
                cross_market_feed.setdefault("warning", "all cross-market quotes failed")
            elif len(usable) < len(symbols) and str(cross_market_state).lower() == "available":
                cross_market_state = "degraded"
                cross_market_feed.setdefault("warning", "some cross-market quotes failed")
    ctrader_required = ["CTRADER_CLIENT_ID", "CTRADER_CLIENT_SECRET", "CTRADER_ACCESS_TOKEN", "CTRADER_ACCOUNT_ID"]
    cme_required = ["CME_API_KEY or CME_CLIENT_ID"]
    options_required = ["CME_API_KEY or OPTIONS_FEED_URL"]
    ctrader_configured = all(os.getenv(k) for k in ctrader_required)
    cme_configured = bool(os.getenv("CME_API_KEY") or os.getenv("CME_CLIENT_ID"))
    options_configured = bool(os.getenv("OPTIONS_FEED_URL") or os.getenv("CME_API_KEY"))

    return {
        "decision_state": status(
            age.get("state"),
            "Decision state",
            source="ifvg_mtf_decision_state.json",
            message=age.get("label", ""),
            age_seconds=age.get("age_seconds"),
            severity=age.get("severity"),
        ),
        "twelvedata": status(
            td_state,
            "Twelve Data candles",
            source="twelvedata_time_series",
            configured=bool(os.getenv("TWELVE_DATA_API_KEY") or os.getenv("GOLD_TWELVE_DATA_API_KEY")),
            message=td_message or f"{candles_loaded} analysis candles loaded.",
        ),
        "chart_fallback": status(
            "chart_only",
            "Chart fallback",
            source="Yahoo GC=F -> local CSV",
            configured=True,
            message="Chart-only fallback is available when Twelve Data is rate-limited; it is not treated as a verified analysis feed.",
            severity="warning",
        ),
        "fmp_macro": status(
            macro,
            "FMP macro calendar",
            source=macro_feed.get("source") or "fmp_economic_calendar",
            configured=bool(os.getenv("FMP_API_KEY") or os.getenv("FINANCIAL_MODELING_PREP_API_KEY")),
            required_env=["FMP_API_KEY"],
            age_seconds=feed_age(macro_feed, macro_file),
            message=feed_message(macro_feed),
        ),
        "finnhub_sentiment": status(
            sentiment,
            "Finnhub/news sentiment",
            source=sentiment_feed.get("source") or "finnhub_forex_news",
            configured=bool(os.getenv("FINNHUB_API_KEY")),
            required_env=["FINNHUB_API_KEY"],
            age_seconds=feed_age(sentiment_feed, sentiment_file),
            message=feed_message(sentiment_feed),
        ),
        "spread": status(
            spread,
            "Spread feed",
            source=spread_feed.get("source") or live_context.get("spread_source") or market.get("spread_source") or "twelvedata_quote",
            configured=bool(os.getenv("TWELVE_DATA_API_KEY") or os.getenv("GOLD_TWELVE_DATA_API_KEY")),
            age_seconds=feed_age(spread_feed, spread_file),
            message=feed_message(spread_feed),
        ),
        "volatility": status(
            _feed_state(live_context, "volatility_state") or _feed_state(market, "volatility_state") or "unknown",
            "M15 ATR volatility",
            source=volatility_feed.get("source") or "twelvedata_M15",
            configured=bool(os.getenv("TWELVE_DATA_API_KEY") or os.getenv("GOLD_TWELVE_DATA_API_KEY")),
            age_seconds=feed_age(volatility_feed, volatility_file),
            message=feed_message(volatility_feed),
        ),
        "cot": status(
            live_context.get("cot_state") or market.get("cot_state") or "unknown",
            "COT positioning",
            source=cot_feed.get("source") or live_context.get("cot_source") or market.get("cot_source") or "fmp_cot",
            configured=bool(os.getenv("FMP_API_KEY") or os.getenv("FINANCIAL_MODELING_PREP_API_KEY")),
            required_env=["FMP_API_KEY"],
            age_seconds=feed_age(cot_feed, cot_file),
            message=feed_message(cot_feed),
        ),
        "cross_market": status(
            cross_market_state,
            "DXY / yields / VIX",
            source=cross_market_feed.get("source") or live_context.get("cross_market_source") or market.get("cross_market_source") or "twelvedata_quote",
            configured=bool(os.getenv("TWELVE_DATA_API_KEY") or os.getenv("GOLD_TWELVE_DATA_API_KEY")),
            age_seconds=feed_age(cross_market_feed, cross_market_file),
            message=feed_message(cross_market_feed),
        ),
        "ctrader": status(
            "pending_or_configured" if ctrader_configured else "missing_credentials",
            "cTrader broker",
            source="ctrader_open_api",
            configured=ctrader_configured,
            required_env=ctrader_required,
            message="Broker execution remains disabled until credentials and live-order policy are both enabled.",
        ),
        "cme": status(
            "credentials_present_not_validated" if cme_configured else "missing_credentials",
            "CME futures feed",
            source="cme_direct_or_vendor",
            configured=cme_configured,
            required_env=cme_required,
            message="No live CME/OI feed is configured. Static market levels are not a substitute for live CME data.",
        ),
        "options": status(
            "credentials_present_not_validated" if options_configured else ("manual_proxy" if levels_file.exists() else "missing_credentials"),
            "Options / IV / skew",
            source="options_vendor_or_market_levels_json",
            configured=options_configured,
            required_env=options_required,
            age_seconds=file_age(levels_file),
            message="Using manual market_levels.json as a proxy until a real options/OI feed is configured." if not options_configured and levels_file.exists() else "",
            severity="warning" if not options_configured else None,
        ),
        "orders": status(
            "unlocked" if os.getenv("GOLD_ENABLE_LIVE_ORDERS", "false").lower() == "true" else "locked",
            "Live orders",
            source="runtime_policy",
            configured=os.getenv("GOLD_EXECUTION_MODE", "paper").lower() == "live",
            message=f"Execution mode: {os.getenv('GOLD_EXECUTION_MODE', 'paper')}.",
        ),
    }


def get_decision_for_api(*, refresh: bool = False) -> dict[str, Any]:
    """Fast read for the web UI; full harden only when requested or state is stale."""
    path = find_decision_path()
    if refresh:
        return harden_decision()
    state = read_json(path, {})
    if state.get("hardened") and state.get("score_decomposition"):
        return state
    if state:
        return harden_decision(state)
    return harden_decision({})


def harden_decision(decision: dict[str, Any] | None = None) -> dict[str, Any]:
    path = find_decision_path()
    state = dict(decision or read_json(path, {}))
    live_context = read_json(LIVE_CONTEXT_PATH, {})
    align = build_tf_alignment(state)
    missing = missing_inputs(state, live_context)
    score = score_decomposition(state, live_context, align, missing)
    age = source_age_status(age_seconds_from_state(state))

    readable_blockers = _dedupe_lines(list(state.get("blockers") or []) + list(state.get("hard_blocks") or []))
    for item in missing:
        label = item["label"] if isinstance(item, dict) else str(item)
        readable_blockers = _dedupe_lines(readable_blockers + [label])

    # Strict readiness: unknown context and stale state cannot be Grade A.
    strict_unknown = os.getenv("GOLD_STRICT_UNKNOWN_CONTEXT", "true").lower() != "false"
    side = norm_side(state.get("side"))
    rr_tp2 = _num(state.get("rr_tp2"), 0)
    grade_allowed = (
        score["total"] >= int(os.getenv("GOLD_GRADE_A_MIN_SCORE", "82"))
        and align["aligned_count"] >= align["required_count"]
        and align["htf_aligned"] >= align["htf_required"]
        and align["entry_confirmed"]
        and align["entry_displacement"]
        and rr_tp2 >= float(os.getenv("GOLD_MIN_RR_TP2", "2.0"))
        and side in {"buy", "sell"}
    )
    if strict_unknown and missing:
        grade_allowed = False

    hardened_action = state.get("action") or "WAIT"
    if not grade_allowed:
        hardened_action = "WAIT_HARD_BLOCK" if missing or readable_blockers else "WAIT"

    state.update({
        "hardened": True,
        "action_raw": state.get("action"),
        "action": hardened_action,
        "final_score_raw": state.get("final_score"),
        "final_score": int(score["total"]),
        "final_grade_raw": state.get("final_grade"),
        "final_grade": state.get("final_grade") if grade_allowed else "D",
        "score_decomposition": score["components"],
        "score_decomposition_total": score["total"],
        "data_quality_penalty": score["data_quality_penalty"],
        "missing_inputs": missing,
        "source_age_status": age,
        "tf_align": align["by_tf"],
        "alignment_audit": {k: v for k, v in align.items() if k != "by_tf"},
        "watching_for": watching_for(state, align, missing),
        "readable_blockers": readable_blockers,
        "readable_reasons": _dedupe_lines(state.get("reasons") or []),
        "provider_health_summary": provider_health(state, live_context),
        "market_intelligence_summary": {
            "macro": normalize_feed_display(_feed_state(live_context, "macro_state") or _feed_state(state.get("market_context") or {}, "macro_state")),
            "sentiment": normalize_feed_display(_feed_state(live_context, "sentiment_state") or _feed_state(state.get("market_context") or {}, "sentiment_state")),
            "spread": normalize_feed_display(
                _feed_state(live_context, "spread_state")
                or ("ok" if (state.get("market_context") or {}).get("spread_points") is not None else "unknown")
            ),
            "volatility": normalize_feed_display(
                _feed_state(live_context, "volatility_state") or _feed_state(state.get("market_context") or {}, "volatility_state")
            ),
            "cme": "configured" if os.getenv("CME_API_KEY") or os.getenv("CME_CLIENT_ID") else "missing credentials",
            "options": "configured" if os.getenv("OPTIONS_FEED_URL") or os.getenv("CME_API_KEY") else "manual proxy" if (ROOT / "config" / "market_levels.json").exists() else "missing credentials",
            "cot": _feed_state(live_context, "cot_state") or "unknown",
            "cross_market": _feed_state(live_context, "cross_market_state") or "unknown",
        },
        "chart_meta": {
            "provider": (state.get("cloud_status") or {}).get("data_provider") or os.getenv("GOLD_MARKET_DATA_PROVIDER", "unknown"),
            "volume_note": "Twelve Data XAU/USD candles may report volume as 0.0; this is expected for this feed.",
        },
        "live_orders_enabled": os.getenv("GOLD_ENABLE_LIVE_ORDERS", "false").lower() == "true",
    })

    write_json(path, state)
    write_json(PROVIDER_HEALTH_PATH, state["provider_health_summary"])
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    write_json(SNAPSHOT_DIR / f"decision_{stamp}.json", state)
    return state


if __name__ == "__main__":
    print(json.dumps(harden_decision(), indent=2, allow_nan=False))
