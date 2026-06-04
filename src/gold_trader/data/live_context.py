from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(
    os.getenv("GOLD_RUNTIME_ROOT", str(Path(__file__).resolve().parents[3]))
).resolve()
LOGS = ROOT / "logs"
DATA = ROOT / "data"
CONTEXT_PATH = LOGS / "live_market_context.json"
SENTIMENT_PATH = LOGS / "sentiment_state.json"
SPREAD_PATH = LOGS / "spread_state.json"
MACRO_PATH = DATA / "macro" / "economic_calendar.json"
COT_PATH = DATA / "cot" / "gold_cot_state.json"
CROSS_MARKET_PATH = LOGS / "cross_market_state.json"

HIGH_IMPACT_TERMS = {
    "cpi", "consumer price index", "ppi", "producer price index", "pce", "nfp",
    "nonfarm", "non-farm", "payroll", "unemployment", "fomc", "federal funds",
    "fed interest", "fed chair", "powell", "retail sales", "ism", "pmi", "gdp",
    "durable goods", "initial jobless", "jobless claims", "jolts", "treasury",
}

GOLD_BULLISH_TERMS = {
    "safe haven", "safe-haven", "risk off", "risk-off", "geopolitical", "war",
    "inflation", "dovish", "rate cut", "lower yields", "recession", "banking stress",
    "weaker dollar", "dollar weak", "usd weak", "yield falls", "yields fall",
}
GOLD_BEARISH_TERMS = {
    "strong dollar", "dollar strong", "usd strong", "hawkish", "rate hike",
    "higher yields", "yield rises", "yields rise", "risk on", "risk-on", "soft landing",
    "disinflation", "strong payrolls", "hot jobs", "hot inflation",
}

@dataclass
class ProviderHealth:
    provider: str
    ok: bool
    source: str
    message: str = ""
    updated_at: str = ""

@dataclass
class LiveMarketContext:
    timestamp_utc: str
    symbol: str
    spread_points: float | None = None
    spread_state: str = "unknown"
    spread_source: str = "none"
    macro_state: str = "unknown"
    macro_source: str = "none"
    macro_blocked_until_utc: str | None = None
    macro_events: list[dict[str, Any]] = field(default_factory=list)
    sentiment_score: float | None = None
    sentiment_state: str = "unknown"
    sentiment_source: str = "none"
    sentiment_summary: str = ""
    cot_state: str = "unknown"
    cot_source: str = "none"
    cot_summary: str = ""
    cross_market_state: str = "unknown"
    cross_market_source: str = "none"
    cross_market_notes: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provider_health: list[dict[str, Any]] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).replace(microsecond=0).isoformat()


def _ensure_dirs() -> None:
    for p in [LOGS, MACRO_PATH.parent, COT_PATH.parent]:
        p.mkdir(parents=True, exist_ok=True)


def _json_get(url: str, *, token_header: tuple[str, str] | None = None, timeout: int = 20) -> Any:
    headers = {"User-Agent": "GoldTrader/1.0"}
    if token_header:
        headers[token_header[0]] = token_header[1]
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _env_key(*names: str) -> str:
    for name in names:
        v = os.getenv(name, "").strip()
        if v:
            return v
    return ""


def update_spread(symbol: str = "XAU/USD") -> tuple[dict[str, Any], ProviderHealth]:
    key = _env_key("TWELVE_DATA_API_KEY", "GOLD_TWELVE_DATA_API_KEY")
    if not key:
        payload = {"state": "unknown", "spread_points": None, "source": "none", "updated_at": _iso(), "warning": "TWELVE_DATA_API_KEY missing"}
        _write(SPREAD_PATH, payload)
        return payload, ProviderHealth("spread", False, "none", payload["warning"], payload["updated_at"])

    # Twelve Data price endpoint is broadly available; bid/ask may be plan/provider-dependent.
    params = urlencode({"symbol": symbol, "apikey": key})
    url = f"https://api.twelvedata.com/quote?{params}"
    try:
        data = _json_get(url)
        bid = data.get("bid") or data.get("bid_price")
        ask = data.get("ask") or data.get("ask_price")
        close = data.get("close") or data.get("price")
        if bid is not None and ask is not None:
            spread = abs(float(ask) - float(bid))
            state = "ok" if spread <= float(os.getenv("GOLD_MAX_SPREAD_POINTS", "1.5")) else "wide"
            payload = {"state": state, "spread_points": spread, "bid": float(bid), "ask": float(ask), "source": "twelvedata_quote", "updated_at": _iso()}
        else:
            payload = {"state": "unknown_nonfatal_in_paper", "spread_points": None, "last_price": float(close) if close is not None else None, "source": "twelvedata_quote", "updated_at": _iso(), "warning": "bid/ask unavailable from provider/plan"}
        _write(SPREAD_PATH, payload)
        return payload, ProviderHealth("spread", True, payload["source"], payload.get("warning", ""), payload["updated_at"])
    except Exception as exc:
        payload = {"state": "unknown", "spread_points": None, "source": "twelvedata_quote", "updated_at": _iso(), "warning": repr(exc)}
        _write(SPREAD_PATH, payload)
        return payload, ProviderHealth("spread", False, "twelvedata_quote", repr(exc), payload["updated_at"])


def update_macro() -> tuple[dict[str, Any], ProviderHealth]:
    key = _env_key("FMP_API_KEY", "FINANCIAL_MODELING_PREP_API_KEY")
    start = _now() - timedelta(hours=12)
    end = _now() + timedelta(days=7)
    if not key:
        payload = {"state": "unknown", "source": "none", "events": [], "updated_at": _iso(), "warning": "FMP_API_KEY missing"}
        _write(MACRO_PATH, payload)
        return payload, ProviderHealth("macro", False, "none", payload["warning"], payload["updated_at"])

    params = urlencode({"from": start.date().isoformat(), "to": end.date().isoformat(), "apikey": key})
    url = f"https://financialmodelingprep.com/stable/economic-calendar?{params}"
    try:
        raw = _json_get(url)
        if isinstance(raw, dict) and "error" in raw:
            raise RuntimeError(json.dumps(raw)[:500])
        events = raw if isinstance(raw, list) else raw.get("data", []) if isinstance(raw, dict) else []
        filtered: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        now = _now()
        window_before = int(os.getenv("GOLD_MACRO_BLOCK_MINUTES_BEFORE", "45"))
        window_after = int(os.getenv("GOLD_MACRO_BLOCK_MINUTES_AFTER", "45"))
        for ev in events:
            country = (ev.get("country") or ev.get("currency") or "").upper()
            title = str(ev.get("event") or ev.get("title") or ev.get("name") or "")
            impact = str(ev.get("impact") or ev.get("importance") or "").lower()
            lower = title.lower()
            if country not in {"US", "USA", "USD", ""}:
                continue
            is_high = any(term in lower for term in HIGH_IMPACT_TERMS) or "high" in impact
            if not is_high:
                continue
            dt_raw = ev.get("date") or ev.get("datetime") or ev.get("time")
            filtered_ev = {
                "date": dt_raw,
                "time_utc": dt_raw,
                "event": title,
                "name": title,
                "country": country or "USD",
                "impact": impact or "high",
            }
            filtered.append(filtered_ev)
            try:
                ev_dt = datetime.fromisoformat(str(dt_raw).replace("Z", "+00:00"))
                if ev_dt.tzinfo is None:
                    ev_dt = ev_dt.replace(tzinfo=timezone.utc)
                if ev_dt - timedelta(minutes=window_before) <= now <= ev_dt + timedelta(minutes=window_after):
                    blockers.append(filtered_ev)
            except Exception:
                pass
        state = "blocked" if blockers else "clear"
        payload = {"state": state, "source": "fmp_economic_calendar", "events": filtered[:50], "active_blockers": blockers[:10], "updated_at": _iso()}
        _write(MACRO_PATH, payload)
        return payload, ProviderHealth("macro", True, "fmp_economic_calendar", f"{len(filtered)} high-impact USD events", payload["updated_at"])
    except Exception as exc:
        payload = {"state": "unknown", "source": "fmp_economic_calendar", "events": [], "updated_at": _iso(), "warning": repr(exc)}
        _write(MACRO_PATH, payload)
        return payload, ProviderHealth("macro", False, "fmp_economic_calendar", repr(exc), payload["updated_at"])


def update_sentiment() -> tuple[dict[str, Any], ProviderHealth]:
    key = _env_key("FINNHUB_API_KEY")
    if not key:
        payload = {"score": None, "state": "unknown", "summary": "FINNHUB_API_KEY missing", "source": "none", "updated_at": _iso()}
        _write(SENTIMENT_PATH, payload)
        return payload, ProviderHealth("sentiment", False, "none", payload["summary"], payload["updated_at"])
    params = urlencode({"category": "forex", "token": key})
    url = f"https://finnhub.io/api/v1/news?{params}"
    try:
        items = _json_get(url)
        if not isinstance(items, list):
            raise RuntimeError(json.dumps(items)[:500])
        score = 0
        hits: list[str] = []
        for item in items[:40]:
            text = f"{item.get('headline','')} {item.get('summary','')}".lower()
            if not any(x in text for x in ["gold", "xau", "dollar", "usd", "yield", "fed", "inflation", "rates", "treasury"]):
                continue
            bull = sum(1 for t in GOLD_BULLISH_TERMS if t in text)
            bear = sum(1 for t in GOLD_BEARISH_TERMS if t in text)
            score += bull - bear
            if bull or bear:
                hits.append(str(item.get("headline") or "").strip())
        norm = max(-1.0, min(1.0, score / 8.0))
        if norm >= 0.35:
            state = "bullish"
        elif norm <= -0.35:
            state = "bearish"
        elif norm > 0.1:
            state = "mild_bullish"
        elif norm < -0.1:
            state = "mild_bearish"
        else:
            state = "neutral"
        summary = "; ".join(hits[:3]) if hits else "No strong gold/USD sentiment signal in recent forex news."
        payload = {"score": round(norm, 3), "state": state, "summary": summary, "source": "finnhub_forex_news", "headlines": hits[:10], "updated_at": _iso()}
        _write(SENTIMENT_PATH, payload)
        return payload, ProviderHealth("sentiment", True, "finnhub_forex_news", state, payload["updated_at"])
    except Exception as exc:
        payload = {"score": None, "state": "unknown", "summary": repr(exc), "source": "finnhub_forex_news", "updated_at": _iso()}
        _write(SENTIMENT_PATH, payload)
        return payload, ProviderHealth("sentiment", False, "finnhub_forex_news", repr(exc), payload["updated_at"])


def update_cot() -> tuple[dict[str, Any], ProviderHealth]:
    key = _env_key("FMP_API_KEY", "FINANCIAL_MODELING_PREP_API_KEY")
    if not key:
        payload = {"state": "unknown", "source": "none", "summary": "FMP_API_KEY missing", "updated_at": _iso()}
        _write(COT_PATH, payload)
        return payload, ProviderHealth("cot", False, "none", payload["summary"], payload["updated_at"])
    symbol = os.getenv("GOLD_COT_SYMBOL", "GC").strip() or "GC"
    params = urlencode({"symbol": symbol, "apikey": key})
    candidates = [
        f"https://financialmodelingprep.com/stable/cot-report?{params}",
        f"https://financialmodelingprep.com/api/v4/commitment_of_traders_report?{params}",
    ]
    last_exc: str = ""
    for url in candidates:
        try:
            raw = _json_get(url)
            rows = raw if isinstance(raw, list) else raw.get("data", []) if isinstance(raw, dict) else []
            if not rows:
                continue
            latest = rows[0]
            text = json.dumps(latest)[:500]
            payload = {"state": "available", "source": "fmp_cot", "symbol": symbol, "latest": latest, "summary": text, "updated_at": _iso()}
            _write(COT_PATH, payload)
            return payload, ProviderHealth("cot", True, "fmp_cot", "latest report loaded", payload["updated_at"])
        except Exception as exc:
            last_exc = repr(exc)
    payload = {"state": "unknown", "source": "fmp_cot", "summary": last_exc or "no rows returned; COT may require paid access", "updated_at": _iso()}
    _write(COT_PATH, payload)
    return payload, ProviderHealth("cot", False, "fmp_cot", payload["summary"], payload["updated_at"])


def update_cross_market() -> tuple[dict[str, Any], ProviderHealth]:
    # Uses Twelve Data quotes/time-series for proxy context. Missing symbols are nonfatal.
    key = _env_key("TWELVE_DATA_API_KEY", "GOLD_TWELVE_DATA_API_KEY")
    symbols = [s.strip() for s in os.getenv("GOLD_CONTEXT_SYMBOLS", "DXY,US10Y,VIX,SPY").split(",") if s.strip()]
    if not key:
        payload = {"state": "unknown", "source": "none", "notes": ["TWELVE_DATA_API_KEY missing"], "updated_at": _iso()}
        _write(CROSS_MARKET_PATH, payload)
        return payload, ProviderHealth("cross_market", False, "none", payload["notes"][0], payload["updated_at"])
    notes: list[str] = []
    quotes: dict[str, Any] = {}
    for sym in symbols:
        try:
            url = "https://api.twelvedata.com/quote?" + urlencode({"symbol": sym, "apikey": key})
            q = _json_get(url)
            quotes[sym] = {k: q.get(k) for k in ["symbol", "name", "close", "percent_change", "change", "datetime"] if k in q}
            pc = q.get("percent_change")
            if pc is not None:
                notes.append(f"{sym} {float(pc):+.2f}%")
        except Exception as exc:
            quotes[sym] = {"error": repr(exc)}
    payload = {"state": "available" if quotes else "unknown", "source": "twelvedata_quote", "symbols": quotes, "notes": notes, "updated_at": _iso()}
    _write(CROSS_MARKET_PATH, payload)
    return payload, ProviderHealth("cross_market", bool(quotes), "twelvedata_quote", ", ".join(notes[:4]), payload["updated_at"])


def build_live_context(symbol: str | None = None) -> LiveMarketContext:
    _ensure_dirs()
    sym = symbol or os.getenv("GOLD_TWELVE_DATA_SYMBOL") or os.getenv("GOLD_SYMBOL", "XAU/USD")
    health: list[ProviderHealth] = []
    spread, h = update_spread(sym); health.append(h)
    macro, h = update_macro(); health.append(h)
    sentiment, h = update_sentiment(); health.append(h)
    cot, h = update_cot(); health.append(h)
    cross, h = update_cross_market(); health.append(h)

    blockers: list[str] = []
    warnings: list[str] = []
    if macro.get("state") == "blocked":
        blockers.append("high-impact USD macro event window is active")
    if spread.get("state") == "wide":
        blockers.append("spread is wider than policy")
    if spread.get("state", "").startswith("unknown"):
        warnings.append("spread unknown; live orders must remain locked")
    if sentiment.get("state") == "unknown":
        warnings.append("sentiment source unavailable")
    if cot.get("state") == "unknown":
        warnings.append("COT source unavailable or plan-gated")

    ctx = LiveMarketContext(
        timestamp_utc=_iso(),
        symbol=sym,
        spread_points=spread.get("spread_points"),
        spread_state=spread.get("state", "unknown"),
        spread_source=spread.get("source", "none"),
        macro_state=macro.get("state", "unknown"),
        macro_source=macro.get("source", "none"),
        macro_events=macro.get("events", [])[:12],
        sentiment_score=sentiment.get("score"),
        sentiment_state=sentiment.get("state", "unknown"),
        sentiment_source=sentiment.get("source", "none"),
        sentiment_summary=sentiment.get("summary", ""),
        cot_state=cot.get("state", "unknown"),
        cot_source=cot.get("source", "none"),
        cot_summary=cot.get("summary", ""),
        cross_market_state=cross.get("state", "unknown"),
        cross_market_source=cross.get("source", "none"),
        cross_market_notes=cross.get("notes", []),
        blockers=blockers,
        warnings=warnings,
        provider_health=[asdict(x) for x in health],
    )
    _write(CONTEXT_PATH, asdict(ctx))
    return ctx


def read_live_context() -> dict[str, Any]:
    return _read(CONTEXT_PATH, {})
