"""OpenAI-backed external market context for IFVG setups.

The research layer is deliberately advisory. It cannot create trade ideas,
place orders, change stops, or enable live trading. It only returns structured
confirmation/warnings for an existing IFVG setup.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
_OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
_DEFAULT_MODEL = "gpt-5.4"
_VALID_MODES = {"off", "soft", "hard"}


SYSTEM_PROMPT = (
    "You are a market-context research assistant for an XAUUSD IFVG trading assistant.\n"
    "You do not give financial advice.\n"
    "You do not place trades.\n"
    "You do not create trade ideas.\n"
    "You only grade and warn on external context for an existing IFVG setup.\n"
    "External research is a grading layer — not a trade-elimination layer.\n"
    "Return supports_trade, should_block_trade, confidence, and warnings for grading.\n"
    "The operator still decides manually; never imply the IFVG setup should disappear.\n"
    "Return strict JSON only.\n"
    "Be conservative.\n"
    "If data is unavailable or uncertain, say unknown.\n"
    "Do not invent CME/options values.\n"
    "Do not overstate confidence.\n"
    "High-impact news risk should produce warnings."
)


@dataclass(frozen=True)
class OpenAIResearchConfig:
    enabled: bool = False
    mode: str = "off"
    model: str = _DEFAULT_MODEL
    cache_minutes: int = 10
    max_calls_per_hour: int = 12
    min_ifvg_score_to_research: int = 65
    block_on_high_news_risk: bool = False
    block_if_external_context_opposes_trade: bool = False


@dataclass(frozen=True)
class RealtimeResearchResult:
    timestamp_utc: str
    symbol: str
    bias: str
    supports_trade: bool
    should_block_trade: bool
    confidence: int
    news_risk: str
    macro: dict[str, str]
    options: dict[str, Any]
    warnings: list[str]
    summary: str
    sources: list[str]
    raw: dict[str, Any] = field(default_factory=dict)


def load_openai_research_config(path: Path | str = "config/openai_research.json") -> OpenAIResearchConfig:
    data: dict[str, Any] = {}
    p = Path(path)
    if p.exists():
        try:
            loaded = json.loads(p.read_text())
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    # Default: soft from config file. Env GOLD_OPENAI_RESEARCH overrides when set.
    env_raw = os.environ.get("GOLD_OPENAI_RESEARCH")
    file_mode = str(data.get("mode") or "soft").strip().lower()
    if env_raw and env_raw.strip().lower() in _VALID_MODES:
        mode = env_raw.strip().lower()
    elif file_mode in _VALID_MODES:
        mode = file_mode
    else:
        mode = "soft"
    env_model = os.environ.get("GOLD_OPENAI_RESEARCH_MODEL", "").strip()
    model = env_model or str(data.get("model") or _DEFAULT_MODEL)
    enabled = bool(data.get("enabled", False)) and mode != "off"
    return OpenAIResearchConfig(
        enabled=enabled,
        mode=mode,
        model=model,
        cache_minutes=_int(data.get("cache_minutes"), 10),
        max_calls_per_hour=_int(data.get("max_calls_per_hour"), 12),
        min_ifvg_score_to_research=_int(data.get("min_ifvg_score_to_research"), 65),
        block_on_high_news_risk=bool(data.get("block_on_high_news_risk", False)),
        block_if_external_context_opposes_trade=bool(data.get("block_if_external_context_opposes_trade", False)),
    )


def run_openai_market_research(
    *,
    symbol: str,
    side: str,
    current_price: float,
    ifvg_zone_low: float,
    ifvg_zone_high: float,
    entry_low: float,
    entry_high: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp3: float,
    technical_score: int,
    checklist_rows: Sequence[dict[str, Any]] | None = None,
    market_levels: Sequence[dict[str, Any]] | None = None,
    config_path: Path | str = "config/openai_research.json",
    cache_path: Path | str = "data/cache/openai_market_research.json",
    force_refresh: bool = False,
    scout_alerts: Sequence[dict[str, Any]] | None = None,
) -> RealtimeResearchResult:
    """Return external context for an existing IFVG setup.

    This function never raises for operational failures. It returns a neutral
    result with a warning when disabled, unconfigured, rate-limited, or when the
    OpenAI call fails.
    """
    config = load_openai_research_config(config_path)
    normalized_side = _normalize_side(side)
    base = _neutral_result(symbol, "OpenAI research disabled.", mode=config.mode)
    if not config.enabled or config.mode == "off":
        return base
    if int(technical_score) < config.min_ifvg_score_to_research:
        return _neutral_result(symbol, "IFVG score below external research threshold.", mode=config.mode)

    key = _cache_key(
        symbol=symbol,
        side=normalized_side,
        current_price=current_price,
        ifvg_zone_low=ifvg_zone_low,
        ifvg_zone_high=ifvg_zone_high,
    )
    cache = _load_cache(cache_path)
    if not force_refresh:
        cached = _cached_result(cache, key, config.cache_minutes)
        if cached is not None:
            return cached

    from ..infra.secrets import resolve_openai_api_key

    api_key = resolve_openai_api_key()
    if not api_key:
        result = _neutral_result(symbol, "External OpenAI research unavailable.", mode=config.mode)
        _store_cache(cache_path, cache, key, result, called=False)
        return result

    if not _under_rate_limit(cache, config.max_calls_per_hour):
        result = _neutral_result(symbol, "External OpenAI research rate limit reached.", mode=config.mode)
        _store_cache(cache_path, cache, key, result, called=False)
        return result

    prompt = _build_user_prompt(
        symbol=symbol,
        side=normalized_side,
        current_price=current_price,
        ifvg_zone_low=ifvg_zone_low,
        ifvg_zone_high=ifvg_zone_high,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        technical_score=technical_score,
        checklist_rows=checklist_rows or (),
        market_levels=market_levels or (),
        scout_alerts=scout_alerts or (),
    )
    try:
        parsed = _call_openai_with_web_search(
            prompt=prompt,
            model=config.model,
            api_key=api_key,
        )
        result = _coerce_result(parsed, symbol=symbol)
        _store_cache(cache_path, cache, key, result, called=True)
        return result
    except (KeyError, json.JSONDecodeError, urllib.error.URLError, TimeoutError, OSError) as exc:
        warning = f"External OpenAI research unavailable: {type(exc).__name__}"
        result = _neutral_result(symbol, warning, mode=config.mode)
        _store_cache(cache_path, cache, key, result, called=False)
        return result


def result_to_dict(result: RealtimeResearchResult, *, enabled: bool | None = None, mode: str | None = None) -> dict[str, Any]:
    out = asdict(result)
    out["enabled"] = enabled if enabled is not None else True
    out["mode"] = mode or result.raw.get("mode") or "off"
    out["last_checked"] = result.timestamp_utc
    return out


def external_context_opposes_side(result: RealtimeResearchResult, side: str) -> bool:
    normalized = _normalize_side(side)
    if result.bias == "bearish_gold" and normalized == "buy":
        return True
    if result.bias == "bullish_gold" and normalized == "sell":
        return True
    return False


def should_external_block(result: RealtimeResearchResult, side: str, config: OpenAIResearchConfig) -> bool:
    """Only hard mode may block. Soft/off never block via external research."""
    if config.mode != "hard":
        return False
    if result.should_block_trade:
        return True
    if config.block_on_high_news_risk and result.news_risk == "high":
        return True
    if config.block_if_external_context_opposes_trade and external_context_opposes_side(result, side):
        return True
    return False


def _build_user_prompt(
    *,
    symbol: str,
    side: str,
    current_price: float,
    ifvg_zone_low: float,
    ifvg_zone_high: float,
    entry_low: float,
    entry_high: float,
    stop_loss: float,
    tp1: float,
    tp2: float,
    tp3: float,
    technical_score: int,
    checklist_rows: Sequence[dict[str, Any]],
    market_levels: Sequence[dict[str, Any]],
    scout_alerts: Sequence[dict[str, Any]] = (),
) -> str:
    alert_lines = []
    for a in list(scout_alerts)[-8:]:
        msg = str(a.get("message") or "").strip()
        watch = str(a.get("watch_for") or "").strip()
        kind = str(a.get("kind") or "").strip()
        zone = a.get("price_zone")
        tf = a.get("timeframe")
        parts = [p for p in (kind, msg, watch) if p]
        if zone and isinstance(zone, (list, tuple)) and len(zone) >= 2:
            parts.append(f"zone {zone[0]}-{zone[1]}")
        if tf:
            parts.append(f"M{tf}")
        if parts:
            alert_lines.append(" · ".join(parts))
    alerts_block = "\n".join(f"- {line}" for line in alert_lines) if alert_lines else "- (none yet)"
    return (
        "Evaluate this existing IFVG setup for external confirmation.\n\n"
        f"Symbol: {symbol}\n"
        f"Side: {side}\n"
        f"Current price: {current_price}\n"
        f"IFVG zone: {ifvg_zone_low} - {ifvg_zone_high}\n"
        f"Entry zone: {entry_low} - {entry_high}\n"
        f"Stop loss: {stop_loss}\n"
        f"TP1: {tp1}\n"
        f"TP2: {tp2}\n"
        f"TP3: {tp3}\n"
        f"Technical IFVG score: {technical_score}\n"
        f"Operator market levels: {json.dumps(list(market_levels), separators=(',', ':'))}\n"
        f"Checklist: {json.dumps(list(checklist_rows), separators=(',', ':'))}\n\n"
        "Prior scout alerts (what to watch, when, and where — use these to focus research):\n"
        f"{alerts_block}\n\n"
        "Research current public context using web search when available:\n"
        "- XAUUSD/gold news (Kitco, Reuters, Bloomberg headlines)\n"
        "- DXY / U.S. Dollar Index direction\n"
        "- U.S. 10Y yield direction\n"
        "- CME gold futures/options/open interest\n"
        "- Investing.com / Barchart gold options context\n"
        "- high-impact USD economic calendar (NFP, CPI, PCE, FOMC)\n"
        "- important round numbers and options strike zones\n\n"
        "Return JSON with: timestamp_utc, symbol, bias, supports_trade, "
        "should_block_trade, confidence, news_risk, macro, options, warnings, summary, sources.\n\n"
        "Decision rules:\n"
        "For a BUY setup: Falling DXY supports buy. Falling yields support buy. "
        "Bullish gold news supports buy. Strong resistance or high call/strike barrier nearby should warn. "
        "High-impact USD news soon should warn or block depending on severity.\n"
        "For a SELL setup: Rising DXY supports sell. Rising yields support sell. "
        "Bearish gold news supports sell. Strong support or high put/strike magnet nearby should warn. "
        "High-impact USD news soon should warn or block depending on severity.\n"
        "Do not say a trade is valid unless the external context supports or at least does not strongly "
        "contradict the IFVG direction. If evidence is mixed, return bias mixed and supports_trade false "
        "unless the setup is still acceptable with warnings.\n\n"
        "Expected JSON schema:\n"
        '{"timestamp_utc":"string","symbol":"string","bias":"bullish_gold | bearish_gold | mixed | unknown",'
        '"supports_trade":true,"should_block_trade":false,"confidence":0,"news_risk":"low | medium | high | unknown",'
        '"macro":{"dxy_bias":"supports_buy | supports_sell | neutral | unknown",'
        '"us10y_bias":"supports_buy | supports_sell | neutral | unknown",'
        '"real_yield_bias":"supports_buy | supports_sell | neutral | unknown"},'
        '"options":{"bias":"bullish_gold | bearish_gold | neutral | unknown",'
        '"important_levels":[],"danger_zones":[],"notes":"string"},'
        '"warnings":[],"summary":"string","sources":[]}'
    )


def _call_openai_with_web_search(*, prompt: str, model: str, api_key: str) -> dict[str, Any]:
    """Try OpenAI Responses API with web search, then fall back to chat JSON."""
    responses_payload = {
        "model": model,
        "tools": [{"type": "web_search_preview"}],
        "input": f"{SYSTEM_PROMPT}\n\n{prompt}\n\nReturn strict JSON only.",
    }
    try:
        req = urllib.request.Request(
            _OPENAI_RESPONSES_URL,
            data=json.dumps(responses_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        text = _extract_response_text(raw)
        return json.loads(text)
    except Exception:
        pass

    chat_payload = {
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        _OPENAI_CHAT_URL,
        data=json.dumps(chat_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    content = raw["choices"][0]["message"]["content"]
    return json.loads(content)


def _extract_response_text(raw: dict[str, Any]) -> str:
    output = raw.get("output")
    if isinstance(output, list):
        chunks: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                        text = part.get("text")
                        if isinstance(text, str):
                            chunks.append(text)
            text = item.get("text")
            if isinstance(text, str):
                chunks.append(text)
        if chunks:
            return "".join(chunks).strip()
    text = raw.get("output_text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    raise KeyError("no response text")


def _coerce_result(data: dict[str, Any], *, symbol: str) -> RealtimeResearchResult:
    macro = data.get("macro") if isinstance(data.get("macro"), dict) else {}
    options = data.get("options") if isinstance(data.get("options"), dict) else {}
    return RealtimeResearchResult(
        timestamp_utc=str(data.get("timestamp_utc") or datetime.now(timezone.utc).isoformat()),
        symbol=str(data.get("symbol") or symbol),
        bias=_choice(str(data.get("bias") or "unknown"), {"bullish_gold", "bearish_gold", "mixed", "unknown"}, "unknown"),
        supports_trade=bool(data.get("supports_trade", False)),
        should_block_trade=bool(data.get("should_block_trade", False)),
        confidence=max(0, min(100, _int(data.get("confidence"), 0))),
        news_risk=_choice(str(data.get("news_risk") or "unknown"), {"low", "medium", "high", "unknown"}, "unknown"),
        macro={
            "dxy_bias": _choice(str(macro.get("dxy_bias") or "unknown"), _macro_values(), "unknown"),
            "us10y_bias": _choice(str(macro.get("us10y_bias") or "unknown"), _macro_values(), "unknown"),
            "real_yield_bias": _choice(str(macro.get("real_yield_bias") or "unknown"), _macro_values(), "unknown"),
        },
        options={
            "bias": _choice(str(options.get("bias") or "unknown"), {"bullish_gold", "bearish_gold", "neutral", "unknown"}, "unknown"),
            "important_levels": _float_list(options.get("important_levels")),
            "danger_zones": _float_list(options.get("danger_zones")),
            "notes": str(options.get("notes") or ""),
        },
        warnings=[str(w)[:300] for w in data.get("warnings", []) if isinstance(w, str)],
        summary=str(data.get("summary") or "")[:1200],
        sources=[str(s)[:300] for s in data.get("sources", []) if isinstance(s, str)],
        raw={"provider": "openai"},
    )


def _neutral_result(symbol: str, warning: str, *, mode: str = "off") -> RealtimeResearchResult:
    return RealtimeResearchResult(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        symbol=symbol,
        bias="unknown",
        supports_trade=False,
        should_block_trade=False,
        confidence=0,
        news_risk="unknown",
        macro={"dxy_bias": "unknown", "us10y_bias": "unknown", "real_yield_bias": "unknown"},
        options={"bias": "unknown", "important_levels": [], "danger_zones": [], "notes": ""},
        warnings=[warning] if warning else [],
        summary="External research unavailable or disabled; use the technical IFVG plan only.",
        sources=[],
        raw={"mode": mode, "provider": "none"},
    )


def _cache_key(*, symbol: str, side: str, current_price: float, ifvg_zone_low: float, ifvg_zone_high: float) -> str:
    now = datetime.now(timezone.utc)
    hour_bucket = now.strftime("%Y%m%d%H")
    return "|".join([
        symbol.upper(),
        side,
        f"{round(float(current_price), 1):.1f}",
        f"{round(float(ifvg_zone_low), 1):.1f}",
        f"{round(float(ifvg_zone_high), 1):.1f}",
        hour_bucket,
    ])


def _load_cache(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"records": {}, "call_log": []}
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {"records": {}, "call_log": []}
    if not isinstance(data, dict):
        return {"records": {}, "call_log": []}
    data.setdefault("records", {})
    data.setdefault("call_log", [])
    return data


def _cached_result(cache: dict[str, Any], key: str, cache_minutes: int) -> RealtimeResearchResult | None:
    record = cache.get("records", {}).get(key)
    if not isinstance(record, dict):
        return None
    created = float(record.get("created_at_epoch") or 0.0)
    if time.time() - created > max(0, cache_minutes) * 60:
        return None
    result = record.get("result")
    if not isinstance(result, dict):
        return None
    coerced = _coerce_result(result, symbol=str(result.get("symbol") or "XAUUSD"))
    return RealtimeResearchResult(
        **{**asdict(coerced), "raw": {**coerced.raw, "cache_hit": True}},
    )


def _store_cache(path: Path | str, cache: dict[str, Any], key: str, result: RealtimeResearchResult, *, called: bool) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    cache.setdefault("records", {})
    cache.setdefault("call_log", [])
    cache["records"][key] = {
        "created_at_epoch": time.time(),
        "result": {k: v for k, v in asdict(result).items() if k != "raw"},
    }
    if called:
        cache["call_log"].append(time.time())
    cutoff = time.time() - 3600
    cache["call_log"] = [t for t in cache.get("call_log", []) if isinstance(t, (int, float)) and t >= cutoff]
    p.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _under_rate_limit(cache: dict[str, Any], max_calls_per_hour: int) -> bool:
    cutoff = time.time() - 3600
    calls = [t for t in cache.get("call_log", []) if isinstance(t, (int, float)) and t >= cutoff]
    return len(calls) < max(0, max_calls_per_hour)


def _normalize_side(side: str) -> str:
    s = str(side).strip().lower()
    if s in {"long", "buy", "bull", "bullish"}:
        return "buy"
    if s in {"short", "sell", "bear", "bearish"}:
        return "sell"
    return s or "unknown"


def _choice(value: str, allowed: set[str], default: str) -> str:
    v = value.strip().lower()
    return v if v in allowed else default


def _macro_values() -> set[str]:
    return {"supports_buy", "supports_sell", "neutral", "unknown"}


def _float_list(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    out: list[float] = []
    for item in value:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
