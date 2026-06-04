from __future__ import annotations

import json
import math
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

_PKG_ROOT = Path(__file__).resolve().parents[3]
ROOT = Path(
    os.getenv("GOLD_TRADER_ROOT", os.getenv("GOLD_RUNTIME_ROOT", str(_PKG_ROOT)))
).resolve()
LOGS = ROOT / "logs"
DATA = ROOT / "data"
MACRO = DATA / "macro"
JOURNAL = DATA / "journal"


def _repo_bases() -> list[Path]:
    bases: list[Path] = []
    for raw in (
        ROOT,
        _PKG_ROOT,
        Path.cwd(),
        Path(os.getenv("RENDER_PROJECT_DIR", "")) if os.getenv("RENDER_PROJECT_DIR") else None,
        Path("/opt/render/project/src"),
    ):
        if not raw:
            continue
        try:
            base = Path(raw).resolve()
        except Exception:
            continue
        if base not in bases:
            bases.append(base)
    return bases


def _decision_candidates() -> list[Path]:
    paths: list[Path] = []
    for base in _repo_bases():
        for rel in (
            ("logs", "ifvg_mtf_decision_state.json"),
            ("logs", "decision_state.json"),
            ("data", "ifvg_mtf_decision_state.json"),
            ("data", "state.json"),
        ):
            candidate = base.joinpath(*rel)
            if candidate not in paths:
                paths.append(candidate)
    return paths
LIVE_CONTEXT_PATH = LOGS / "live_market_context.json"
PROVIDER_HEALTH_PATH = LOGS / "provider_health.json"
CALENDAR_PATH = MACRO / "economic_calendar.json"
SNAPSHOTS_PATH = JOURNAL / "decision_snapshots.jsonl"

TFS = ["D1", "H4", "H1", "M30", "M15", "M5", "M1"]
HTFS = ["D1", "H4", "H1"]
ENTRY_TFS = ["M5", "M1"]

COMPONENT_MAX = {
    "timeframe_alignment": 25,
    "ifvg_geometry": 20,
    "macro_regime": 20,
    "sentiment_gate": 15,
    "session_spread": 10,
    "volatility": 10,
}

HIGH_IMPACT_TERMS = [
    "cpi", "pce", "nfp", "nonfarm", "payroll", "fomc", "fed interest", "fed chair",
    "powell", "gdp", "retail sales", "ism", "pmi", "jolts", "unemployment",
    "jobless", "ppi", "treasury", "rate decision", "inflation",
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except Exception:
            return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: _sanitize_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(v) for v in value]
    return value


def read_json(path: Path, default: Any = None) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _sanitize_json(data)
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False, allow_nan=False))
    tmp.replace(path)


def append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")


def find_decision_path() -> Path | None:
    existing = [p for p in _decision_candidates() if p.exists()]
    if not existing:
        return None
    return max(existing, key=lambda p: p.stat().st_mtime)


def load_decision() -> tuple[dict[str, Any], Path | None]:
    path = find_decision_path()
    if not path:
        return {}, None
    data = read_json(path, {})
    return data if isinstance(data, dict) else {}, path


def safe_num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default


def timeframe_map(decision: dict[str, Any]) -> dict[str, dict[str, Any]]:
    reads = decision.get("timeframe_reads") or []
    out: dict[str, dict[str, Any]] = {}
    for item in reads:
        if isinstance(item, dict) and item.get("timeframe"):
            out[str(item["timeframe"]).upper()] = item
    return out


def side_from_decision(decision: dict[str, Any]) -> str:
    side = str(decision.get("side") or "none").lower()
    if side in {"buy", "sell"}:
        return side
    action = str(decision.get("action") or "").lower()
    if "buy" in action:
        return "buy"
    if "sell" in action:
        return "sell"
    return "none"


def tf_bias_state(item: dict[str, Any], side: str) -> str:
    bias = str(item.get("bias") or "unknown").lower()
    if bias in {"bullish", "buy"}:
        return "bullish"
    if bias in {"bearish", "sell"}:
        return "bearish"
    if bias in {"mixed", "range", "neutral"}:
        return "neutral"
    return "unknown"


def tf_aligned(item: dict[str, Any], side: str, *, entry: bool = False) -> bool:
    if side not in {"buy", "sell"}:
        return False
    bias = tf_bias_state(item, side)
    if side == "buy" and bias != "bullish":
        return False
    if side == "sell" and bias != "bearish":
        return False
    ifvg_side = str(item.get("ifvg_side") or "none").lower()
    if entry and ifvg_side != side:
        return False
    # Non-entry HTFs can align by bias alone; IFVG side is reported separately.
    return True


def build_tf_alignment(decision: dict[str, Any]) -> dict[str, Any]:
    side = side_from_decision(decision)
    tfs = timeframe_map(decision)
    tf_align: dict[str, str] = {}
    details: dict[str, dict[str, Any]] = {}
    aligned_count = 0
    htf_aligned = 0
    active_ifvg = 0
    entry_ifvg = False
    entry_displacement = False
    liquidity_ok = False

    for tf in TFS:
        item = tfs.get(tf, {})
        bias_state = tf_bias_state(item, side)
        ifvg_side = str(item.get("ifvg_side") or "none").lower()
        entry = tf in ENTRY_TFS
        aligned = tf_aligned(item, side, entry=entry)
        if aligned:
            aligned_count += 1
        if tf in HTFS and aligned:
            htf_aligned += 1
        if ifvg_side in {"buy", "sell"}:
            active_ifvg += 1
        if entry and ifvg_side == side:
            entry_ifvg = True
        if entry and bool(item.get("displacement")):
            entry_displacement = True
        if bool(item.get("liquidity_sweep")):
            liquidity_ok = True
        tf_align[tf] = bias_state
        details[tf] = {
            "bias": bias_state,
            "ifvg_side": ifvg_side,
            "candles": int(safe_num(item.get("candles"), 0)),
            "score": safe_num(item.get("score"), 0),
            "aligned": aligned,
            "displacement": bool(item.get("displacement")),
            "liquidity_sweep": bool(item.get("liquidity_sweep")),
        }

    return {
        "side": side,
        "tf_align": tf_align,
        "tf_details": details,
        "aligned_count": aligned_count,
        "htf_aligned_count": htf_aligned,
        "active_ifvg_count": active_ifvg,
        "entry_ifvg_confirmed": entry_ifvg,
        "entry_displacement_confirmed": entry_displacement,
        "liquidity_sweep_confirmed": liquidity_ok,
        "required_aligned": int(os.getenv("GOLD_REQUIRED_ALIGNED_TFS", "5")),
        "required_htf_aligned": int(os.getenv("GOLD_REQUIRED_HTF_TFS", "2")),
    }


def decision_age_seconds(decision: dict[str, Any], source: Path | None) -> int | None:
    dt = parse_dt(decision.get("timestamp_utc") or decision.get("updated_at"))
    if dt:
        return max(0, int((now_utc() - dt).total_seconds()))
    if source and source.exists():
        return max(0, int(time.time() - source.stat().st_mtime))
    return None


def age_status(age: int | None) -> str:
    if age is None:
        return "unknown"
    if age < 60:
        return "fresh"
    if age <= 180:
        return "borderline"
    return "stale"


def load_live_context() -> dict[str, Any]:
    ctx = read_json(LIVE_CONTEXT_PATH, {})
    if not isinstance(ctx, dict):
        ctx = {}
    return ctx


def macro_state(decision: dict[str, Any], context: dict[str, Any]) -> tuple[str, list[str]]:
    candidates = [
        context.get("macro_state"),
        (context.get("macro") or {}).get("state") if isinstance(context.get("macro"), dict) else None,
        (decision.get("market_context") or {}).get("macro_state") if isinstance(decision.get("market_context"), dict) else None,
    ]
    state = next((str(x).lower() for x in candidates if x not in (None, "")), "unknown")
    notes: list[str] = []
    if state in {"unknown", "unavailable", "missing"}:
        notes.append("macro calendar unavailable")
    if state in {"blocked", "high_impact", "red"}:
        notes.append("macro hard-block active")
    return state, notes


def sentiment_state(decision: dict[str, Any], context: dict[str, Any]) -> tuple[str, list[str]]:
    sent = context.get("sentiment") if isinstance(context.get("sentiment"), dict) else {}
    candidates = [
        context.get("sentiment_state"), sent.get("state"),
        (decision.get("market_context") or {}).get("sentiment_state") if isinstance(decision.get("market_context"), dict) else None,
    ]
    state = next((str(x).lower() for x in candidates if x not in (None, "")), "unknown")
    notes: list[str] = []
    if state in {"unknown", "unavailable", "missing"}:
        notes.append("sentiment unavailable")
    return state, notes


def volatility_state(decision: dict[str, Any], context: dict[str, Any]) -> tuple[str, list[str]]:
    candidates = [
        context.get("volatility_state"),
        (context.get("volatility") or {}).get("state") if isinstance(context.get("volatility"), dict) else None,
        (decision.get("market_context") or {}).get("volatility_state") if isinstance(decision.get("market_context"), dict) else None,
    ]
    state = next((str(x).lower() for x in candidates if x not in (None, "")), "unknown")
    notes: list[str] = []
    if state in {"unknown", "unavailable", "missing"}:
        notes.append("volatility unavailable")
    if state in {"extreme", "blocked", "too_high", "too_low"}:
        notes.append(f"volatility block: {state}")
    return state, notes


def spread_state(decision: dict[str, Any], context: dict[str, Any]) -> tuple[str, list[str], float | None]:
    spread = None
    for source in [context, context.get("spread") if isinstance(context.get("spread"), dict) else {}, decision.get("market_context") if isinstance(decision.get("market_context"), dict) else {}]:
        if not isinstance(source, dict):
            continue
        for key in ("spread_points", "spread", "value"):
            if source.get(key) is not None:
                spread = safe_num(source.get(key), -1)
                if spread >= 0:
                    break
        if spread is not None and spread >= 0:
            break
    state = "ok" if spread is not None and spread >= 0 else "unknown"
    notes = [] if state == "ok" else ["spread unavailable"]
    max_spread = safe_num(os.getenv("GOLD_MAX_SPREAD_POINTS", "80"), 80)
    if spread is not None and spread > max_spread:
        state = "blocked"
        notes.append(f"spread too wide: {spread:.2f} > {max_spread:.2f}")
    return state, notes, spread


def compute_score(decision: dict[str, Any], context: dict[str, Any], alignment: dict[str, Any], age: int | None) -> dict[str, Any]:
    missing: list[str] = []
    hard_blocks: list[str] = []
    warnings: list[str] = []

    req = alignment["required_aligned"]
    req_htf = alignment["required_htf_aligned"]
    aligned = alignment["aligned_count"]
    htf = alignment["htf_aligned_count"]

    alignment_score = 0
    if aligned >= req:
        alignment_score += 15
    else:
        warnings.append(f"only {aligned}/7 timeframes align; need {req}")
    if htf >= req_htf:
        alignment_score += 10
    else:
        warnings.append(f"only {htf}/3 HTFs align; need {req_htf}")
    alignment_score = min(COMPONENT_MAX["timeframe_alignment"], alignment_score)

    geometry_score = 0
    if alignment["entry_ifvg_confirmed"]:
        geometry_score += 8
    else:
        hard_blocks.append("entry timeframe IFVG not confirmed")
    if alignment["entry_displacement_confirmed"]:
        geometry_score += 6
    else:
        hard_blocks.append("entry displacement not confirmed")
    if alignment["liquidity_sweep_confirmed"]:
        geometry_score += 4
    else:
        warnings.append("no aligned liquidity sweep")
    rr_tp2 = safe_num(decision.get("rr_tp2"), 0)
    min_rr = safe_num(os.getenv("GOLD_MIN_RR_TP2", "2.0"), 2.0)
    if rr_tp2 >= min_rr:
        geometry_score += 2
    else:
        hard_blocks.append(f"RR to TP2 below policy: {rr_tp2:.2f} < {min_rr:.2f}")
    geometry_score = min(COMPONENT_MAX["ifvg_geometry"], geometry_score)

    macro, macro_notes = macro_state(decision, context)
    if macro in {"clear", "mixed", "neutral", "ok"}:
        macro_score = 20 if macro in {"clear", "ok"} else 12
    elif macro in {"blocked", "high_impact", "red"}:
        macro_score = 0
        hard_blocks.append("macro hard-block active")
    else:
        macro_score = 0
        missing.append("macro")
    warnings.extend(macro_notes)

    sent, sent_notes = sentiment_state(decision, context)
    if sent in {"aligned", "bullish", "bearish", "mild_bullish", "mild_bearish", "neutral", "mixed"}:
        # Direction-aware sentiment can be improved later; neutral/mixed gets partial credit.
        sent_score = 15 if sent in {"aligned", "bullish", "bearish", "mild_bullish", "mild_bearish"} else 8
    elif sent in {"opposed", "blocked"}:
        sent_score = 0
        hard_blocks.append("sentiment conflicts with trade side")
    else:
        sent_score = 0
        missing.append("sentiment")
    warnings.extend(sent_notes)

    spread, spread_notes, spread_value = spread_state(decision, context)
    session = str((decision.get("market_context") or {}).get("session") or context.get("session") or "unknown").lower()
    session_ok = session in {"london", "new_york", "ny", "overlap", "killzone", "active"}
    session_score = 0
    if session_ok:
        session_score += 4
    else:
        warnings.append(f"session not preferred: {session}")
    if spread == "ok":
        session_score += 6
    elif spread == "blocked":
        hard_blocks.extend(spread_notes)
    else:
        missing.append("spread")
        warnings.extend(spread_notes)
    session_score = min(COMPONENT_MAX["session_spread"], session_score)

    vol, vol_notes = volatility_state(decision, context)
    if vol in {"normal", "tradable", "ok"}:
        vol_score = 10
    elif vol in {"low", "high", "mixed"}:
        vol_score = 5
    elif vol in {"extreme", "blocked", "too_high", "too_low"}:
        vol_score = 0
        hard_blocks.extend(vol_notes)
    else:
        vol_score = 0
        missing.append("volatility")
    warnings.extend(vol_notes)

    max_age = int(os.getenv("GOLD_MAX_DECISION_AGE_SECONDS", "300"))
    if age is None:
        missing.append("source_age")
    elif age > max_age:
        hard_blocks.append(f"decision state stale: {age}s > {max_age}s")

    components = {
        "timeframe_alignment": alignment_score,
        "ifvg_geometry": geometry_score,
        "macro_regime": macro_score,
        "sentiment_gate": sent_score,
        "session_spread": session_score,
        "volatility": vol_score,
    }
    raw = sum(components.values())
    penalty_per_missing = int(os.getenv("GOLD_MISSING_INPUT_PENALTY", "8"))
    data_quality_penalty = min(35, len(set(missing)) * penalty_per_missing)
    final = max(0, min(100, raw - data_quality_penalty))
    grade_allowed = final >= int(os.getenv("GOLD_GRADE_A_SCORE", "82")) and not hard_blocks

    return {
        "score_decomposition": components,
        "score_component_max": COMPONENT_MAX,
        "raw_score_before_penalty": raw,
        "data_quality_penalty": data_quality_penalty,
        "missing_inputs": sorted(set(missing)),
        "hard_blocks": hard_blocks,
        "warnings": sorted(set([w for w in warnings if w])),
        "strict_final_score": final,
        "strict_grade_allowed": grade_allowed,
        "strict_grade": grade_from_score(final, grade_allowed),
        "macro_state": macro,
        "sentiment_state": sent,
        "spread_state": spread,
        "spread_points": spread_value,
        "volatility_state": vol,
    }


def grade_from_score(score: int, allowed: bool) -> str:
    if not allowed:
        if score >= 70:
            return "B-LOCKED"
        if score >= 50:
            return "C"
        return "D"
    if score >= 92:
        return "A+"
    if score >= 82:
        return "A"
    return "B"


def build_watching_for(alignment: dict[str, Any], score: dict[str, Any]) -> list[str]:
    items: list[str] = []
    if alignment["aligned_count"] < alignment["required_aligned"]:
        items.append(f"{alignment['required_aligned']}th TF alignment")
    if alignment["htf_aligned_count"] < alignment["required_htf_aligned"]:
        items.append("HTF bias confirmation")
    if not alignment["entry_ifvg_confirmed"]:
        items.append("entry TF IFVG retest")
    if not alignment["entry_displacement_confirmed"]:
        items.append("entry displacement candle")
    if not alignment["liquidity_sweep_confirmed"]:
        items.append("liquidity sweep confirmation")
    for missing in score.get("missing_inputs", []):
        items.append(f"{missing} feed healthy")
    if score.get("hard_blocks"):
        items.append("hard-block cleared")
    if not items:
        items.append("operator confirmation in paper mode")
    return items[:10]


def provider_health(decision: dict[str, Any], context: dict[str, Any], source: Path | None, age: int | None) -> dict[str, Any]:
    tfs = timeframe_map(decision)
    candle_total = sum(int(safe_num((tfs.get(tf) or {}).get("candles"), 0)) for tf in TFS)
    health = {
        "timestamp_utc": now_utc().isoformat(timespec="seconds"),
        "decision_source": str(source) if source else None,
        "decision_age_seconds": age,
        "decision_age_status": age_status(age),
        "twelve_data": {
            "configured": bool(os.getenv("TWELVE_DATA_API_KEY")),
            "symbol": os.getenv("GOLD_TWELVE_DATA_SYMBOL") or os.getenv("GOLD_SYMBOL", "XAU/USD"),
            "candles_loaded": candle_total,
            "status": "ok" if candle_total > 0 else "no_candles",
        },
        "fmp_macro": {
            "configured": bool(os.getenv("FMP_API_KEY")),
            "calendar_file_exists": CALENDAR_PATH.exists(),
            "status": "ok" if CALENDAR_PATH.exists() else ("missing_file" if os.getenv("FMP_API_KEY") else "missing_key"),
        },
        "finnhub_sentiment": {
            "configured": bool(os.getenv("FINNHUB_API_KEY")),
            "status": "configured" if os.getenv("FINNHUB_API_KEY") else "missing_key",
        },
        "ctrader": {
            "configured": all(os.getenv(k) for k in ["CTRADER_CLIENT_ID", "CTRADER_CLIENT_SECRET", "CTRADER_ACCESS_TOKEN", "CTRADER_ACCOUNT_ID"]),
            "environment": os.getenv("CTRADER_ENVIRONMENT", "demo"),
            "status": "pending_or_unverified",
        },
        "cme_options": {
            "configured": bool(os.getenv("CME_API_KEY") or os.getenv("CME_CLIENT_ID")),
            "status": "not_connected" if not (os.getenv("CME_API_KEY") or os.getenv("CME_CLIENT_ID")) else "credentials_present_not_validated",
        },
        "orders": {
            "execution_mode": os.getenv("GOLD_EXECUTION_MODE", "paper"),
            "live_orders_enabled": os.getenv("GOLD_ENABLE_LIVE_ORDERS", "false").lower() == "true",
            "status": "unlocked" if os.getenv("GOLD_ENABLE_LIVE_ORDERS", "false").lower() == "true" else "locked",
        },
    }
    write_json(PROVIDER_HEALTH_PATH, health)
    return health


def harden_decision_state(*, write: bool = True, journal: bool = True) -> dict[str, Any]:
    decision, source = load_decision()
    if not decision:
        payload = {
            "timestamp_utc": now_utc().isoformat(timespec="seconds"),
            "action": "WAIT",
            "side": "none",
            "final_score": 0,
            "final_grade": "D",
            "error": "no decision state found",
        }
        if write:
            write_json(LOGS / "ifvg_mtf_decision_state.json", payload)
        return payload

    context = load_live_context()
    age = decision_age_seconds(decision, source)
    align = build_tf_alignment(decision)
    score = compute_score(decision, context, align, age)
    watching = build_watching_for(align, score)
    health = provider_health(decision, context, source, age)

    original_action = str(decision.get("action") or "WAIT")
    strict_allowed = bool(score["strict_grade_allowed"])
    live_enabled = os.getenv("GOLD_ENABLE_LIVE_ORDERS", "false").lower() == "true"
    paper_mode = os.getenv("GOLD_EXECUTION_MODE", "paper").lower() != "live" or not live_enabled

    if strict_allowed:
        action = "TRADE_READY_PAPER_AUTO_ALERT_AUTO" if paper_mode else f"TRADE_READY_LIVE_{align['side'].upper()}"
        next_update = "Paper trade ready; live orders remain locked unless explicitly enabled." if paper_mode else "Live trade ready under active policy."
    else:
        action = "WAIT_HARD_BLOCK" if score.get("hard_blocks") else "WAIT"
        next_update = "Wait for: " + ", ".join(watching[:5]) + "."

    enriched = dict(decision)
    enriched.update({
        "action_raw": original_action,
        "action": action,
        "final_score_raw": decision.get("final_score"),
        "final_grade_raw": decision.get("final_grade"),
        "final_score": score["strict_final_score"],
        "final_grade": score["strict_grade"],
        "grade_allowed": strict_allowed,
        "score_decomposition": score["score_decomposition"],
        "score_component_max": score["score_component_max"],
        "raw_score_before_penalty": score["raw_score_before_penalty"],
        "data_quality_penalty": score["data_quality_penalty"],
        "missing_inputs": score["missing_inputs"],
        "hard_blocks": score["hard_blocks"],
        "warnings": score["warnings"],
        "tf_align": align["tf_align"],
        "tf_alignment_audit": align,
        "watching_for": watching,
        "source_age_seconds": age,
        "source_age_status": age_status(age),
        "provider_health": health,
        "next_update": next_update,
        "market_context": {
            **(decision.get("market_context") if isinstance(decision.get("market_context"), dict) else {}),
            "macro_state": score["macro_state"],
            "sentiment_state": score["sentiment_state"],
            "spread_state": score["spread_state"],
            "spread_points": score["spread_points"],
            "volatility_state": score["volatility_state"],
        },
    })

    if write:
        target = source or (LOGS / "ifvg_mtf_decision_state.json")
        write_json(target, enriched)
        write_json(DATA / "state.json", enriched)
    if journal:
        snap = {k: enriched.get(k) for k in ["timestamp_utc", "action", "side", "final_score", "final_grade", "current_price", "entry_low", "entry_high", "stop_loss", "tp1", "tp2", "tp3", "source_age_seconds"]}
        snap["missing_inputs"] = enriched.get("missing_inputs", [])
        snap["hard_blocks"] = enriched.get("hard_blocks", [])
        append_jsonl(SNAPSHOTS_PATH, snap)
    return enriched


def fetch_fmp_calendar(days_ahead: int = 7) -> dict[str, Any]:
    key = os.getenv("FMP_API_KEY", "").strip()
    if not key:
        payload = {"status": "missing_key", "events": [], "updated_at": now_utc().isoformat(timespec="seconds")}
        write_json(CALENDAR_PATH, payload)
        return payload
    start = now_utc().date()
    end = start + timedelta(days=days_ahead)
    params = urllib.parse.urlencode({"from": str(start), "to": str(end), "apikey": key})
    urls = [
        f"https://financialmodelingprep.com/stable/economic-calendar?{params}",
        f"https://financialmodelingprep.com/api/v3/economic_calendar?{params}",
    ]
    last_error = None
    raw_items: list[Any] = []
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=25) as response:
                raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            if isinstance(data, list):
                raw_items = data
                break
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                raw_items = data["data"]
                break
            last_error = data
        except Exception as exc:
            last_error = repr(exc)

    events = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("event") or item.get("title") or item.get("name") or "")
        currency = str(item.get("currency") or item.get("country") or "").upper()
        impact = str(item.get("impact") or item.get("importance") or "").lower()
        text = title.lower()
        high = any(term in text for term in HIGH_IMPACT_TERMS) or impact in {"high", "3", "red"}
        if currency and "USD" not in currency and "UNITED STATES" not in currency:
            continue
        events.append({
            "time": item.get("date") or item.get("datetime") or item.get("time"),
            "event": title,
            "currency": currency or "USD",
            "impact": "high" if high else (impact or "unknown"),
            "actual": item.get("actual"),
            "estimate": item.get("estimate") or item.get("forecast"),
            "previous": item.get("previous"),
            "trade_block": bool(high),
        })
    payload = {
        "status": "ok" if raw_items else "failed",
        "updated_at": now_utc().isoformat(timespec="seconds"),
        "source": "fmp",
        "error": None if raw_items else last_error,
        "events": events,
    }
    write_json(CALENDAR_PATH, payload)
    return payload
