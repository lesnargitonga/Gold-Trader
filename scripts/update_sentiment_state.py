#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "logs" / "sentiment_state.json"


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _http_get(url: str, timeout: int = 10) -> Any:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


DRIVER_PATTERNS = {
    # bullish drivers for gold
    "usd_weak": ["usd weak", "dollar weak", "weak dollar", "dollar falls", "greenback falls", "usd retreats", "dollar slides"],
    "yields_fall": ["yields fall", "yields decline", "10-year yield falls", "rates fall", "yields drop", "treasury yields fall"],
    "inflation_risk": ["inflation", "consumer prices", "cpi", "inflation fears", "inflation risk"],
    "geopolitical": ["geopolit", "war", "conflict", "sanctions", "tension", "escalat"],
    "risk_off": ["risk-off", "risk off", "safe haven", "risk aversion", "flight to quality"],
    "fed_dovish": ["fed dovish", "fed hold", "no rate hike", "fed pause", "rate cut"],
    # bearish drivers
    "usd_strong": ["dollar rises", "usd strength", "strong dollar", "dollar rallies", "greenback rally"],
    "yields_rise": ["yields rise", "rates rise", "10-year yield rises", "yields higher", "rates higher"],
    "fed_hawkish": ["fed hawkish", "rate hike", "fed raises", "tightening"],
    "risk_on": ["stocks rally", "equities rally", "risk-on", "risk on", "risk appetite"],
    "inflation_cooling": ["inflation cools", "inflation eases", "cpi falls", "disinflation"],
}

DRIVER_LABEL = {
    "usd_weak": "USD weakness",
    "yields_fall": "lower yields",
    "inflation_risk": "inflation risk",
    "geopolitical": "geopolitical risk",
    "risk_off": "risk-off / safe-haven",
    "fed_dovish": "Fed dovish",
    "usd_strong": "USD strength",
    "yields_rise": "higher yields",
    "fed_hawkish": "Fed hawkish",
    "risk_on": "risk-on / equities",
    "inflation_cooling": "inflation cooling",
}

DRIVER_WEIGHTS = {
    "usd_weak": 0.18,
    "yields_fall": 0.18,
    "inflation_risk": 0.12,
    "geopolitical": 0.12,
    "risk_off": 0.12,
    "fed_dovish": 0.18,
    "usd_strong": -0.18,
    "yields_rise": -0.18,
    "fed_hawkish": -0.18,
    "risk_on": -0.12,
    "inflation_cooling": -0.12,
}


def detect_drivers(text: str) -> list[str]:
    txt = (text or "").lower()
    found: list[str] = []
    for k, patterns in DRIVER_PATTERNS.items():
        for p in patterns:
            if p in txt:
                found.append(k)
                break
    return found


def headline_score_from_drivers(drivers: list[str]) -> float:
    s = 0.0
    seen = set()
    for d in drivers:
        if d in DRIVER_WEIGHTS and d not in seen:
            s += DRIVER_WEIGHTS[d]
            seen.add(d)
    # clamp per-headline
    if s > 0.5:
        s = 0.5
    if s < -0.5:
        s = -0.5
    return s


def parse_item_to_headline(item: dict) -> dict:
    # Finnhub and other sources have varying keys
    title = item.get("headline") or item.get("title") or item.get("summary") or ""
    source = item.get("source") or item.get("provider") or item.get("site") or "unknown"
    dt = None
    for key in ("datetime", "time", "publishedAt", "published_at"):
        if item.get(key) is not None:
            try:
                v = item.get(key)
                if isinstance(v, (int, float)):
                    dt = datetime.utcfromtimestamp(int(v)).replace(tzinfo=timezone.utc).isoformat()
                else:
                    dt = str(v)
                break
            except Exception:
                continue
    text = title + " " + (item.get("summary") or "")
    drivers = detect_drivers(text)
    hs = headline_score_from_drivers(drivers)
    driver_label = DRIVER_LABEL.get(drivers[0], "") if drivers else ""
    return {"title": title, "source": source, "datetime": dt, "score": round(hs, 3), "driver": driver_label, "drivers": drivers}


def fetch_finnhub_gold_sentiment(api_key: str) -> dict[str, Any]:
    # Attempt multiple endpoints: news categories and company-news; aggregate best-effort
    from datetime import datetime, timedelta

    now_dt = datetime.utcnow()
    frm = (now_dt - timedelta(days=2)).strftime("%Y-%m-%d")
    to = now_dt.strftime("%Y-%m-%d")

    candidates: list[dict] = []
    errors: list[str] = []

    for category in ("forex", "general"):
        try:
            url = f"https://finnhub.io/api/v1/news?category={category}&token={api_key}"
            data = _http_get(url)
            if isinstance(data, list):
                candidates.extend(data)
        except Exception as exc:
            errors.append(str(exc))

    try:
        symbols = ("GC=F", "XAUUSD", "XAU")
        for s in symbols:
            try:
                url = f"https://finnhub.io/api/v1/company-news?symbol={s}&from={frm}&to={to}&token={api_key}"
                data = _http_get(url)
                if isinstance(data, list):
                    candidates.extend(data)
            except Exception:
                continue
    except Exception:
        pass

    # Build headline items with filtering on gold-related content
    items: list[dict] = []
    for item in candidates:
        title = item.get("headline") or item.get("title") or item.get("summary") or ""
        if not title:
            continue
        lt = title.lower()
        if "gold" in lt or "xau" in lt or "gc=" in lt:
            items.append(item)

    return {"items": items, "errors": errors}


def build_sentiment_from_items(items: list[dict]) -> dict:
    if not items:
        return {"state": "unknown", "score": 0, "confidence": 0, "fresh": False, "error": "no usable headlines"}

    parsed = [parse_item_to_headline(it) for it in items]
    # overall score is sum of headline scores, clamped
    total = sum(p.get("score", 0.0) for p in parsed)
    total = max(-1.0, min(1.0, total))

    # aggregate drivers
    agg = {}
    for p in parsed:
        for d in p.get("drivers", []):
            agg[d] = agg.get(d, 0) + 1
    drivers = [DRIVER_LABEL.get(k, k) for k in sorted(agg.keys(), key=lambda x: -agg[x])]

    # identify conflicts: both bullish and bearish drivers present
    bullish_keys = {"usd_weak", "yields_fall", "inflation_risk", "geopolitical", "risk_off", "fed_dovish"}
    bearish_keys = {"usd_strong", "yields_rise", "fed_hawkish", "risk_on", "inflation_cooling"}
    has_bull = any(k in agg for k in bullish_keys)
    has_bear = any(k in agg for k in bearish_keys)
    conflicts = []
    if has_bull and has_bear:
        conflicts.append("mixed drivers present")

    # confidence heuristic
    confidence = min(0.98, 0.2 + 0.18 * min(len(parsed), 4) + 0.05 * min(len(drivers), 5))

    # map score to descriptive state
    if total >= 0.5:
        state = "strong_bullish"
    elif total >= 0.15:
        state = "mild_bullish"
    elif total <= -0.5:
        state = "strong_bearish"
    elif total <= -0.15:
        state = "mild_bearish"
    else:
        state = "neutral"

    return {
        "state": state,
        "score": round(total, 3),
        "confidence": round(confidence, 3),
        "fresh": True,
        "source": "finnhub",
        "updated_at": now(),
        "drivers": drivers,
        "conflicts": conflicts,
        "headlines": parsed,
    }


def main() -> None:
    api_key = os.getenv("FINNHUB_API_KEY", "").strip()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if not api_key:
        payload = {"state": "unknown", "score": 0, "confidence": 0, "fresh": False, "error": "no FINNHUB_API_KEY"}
        OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps(payload, indent=2))
        return

    try:
        data = fetch_finnhub_gold_sentiment(api_key)
        items = data.get("items", []) if isinstance(data, dict) else []
    except Exception as exc:
        payload = {"state": "unknown", "score": 0, "confidence": 0, "fresh": False, "error": repr(exc)}
        OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(json.dumps(payload, indent=2))
        return

    payload = build_sentiment_from_items(items)
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
