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

def fetch_finnhub_gold_sentiment(api_key: str) -> dict[str, Any]:
    # Try Finnhub news endpoints and filter for gold-related headlines
    from datetime import datetime, timedelta

    now_dt = datetime.utcnow()
    frm = (now_dt - timedelta(days=2)).strftime("%Y-%m-%d")
    to = now_dt.strftime("%Y-%m-%d")

    candidates: list[dict] = []
    errors: list[str] = []

    for category in ("forex", "general"):
        try:
            url = f"https://finnhub.io/api/v1/news?category={category}&token={api_key}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                candidates.extend(data)
        except Exception as exc:
            errors.append(str(exc))

    # fallback: try a company-news style endpoint for common symbols (best-effort)
    try:
        symbols = ("GC=F", "XAUUSD", "XAU")
        for s in symbols:
            url = f"https://finnhub.io/api/v1/company-news?symbol={s}&from={frm}&to={to}&token={api_key}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                candidates.extend(data)
    except Exception:
        pass

    headlines: list[str] = []
    for item in candidates:
        h = item.get("headline") or item.get("summary") or item.get("title") or ""
        if not h:
            continue
        lh = h.lower()
        if "gold" in lh or "xau" in lh or "gc=" in lh:
            headlines.append(h)

    return {"headlines": headlines, "errors": errors}

def classify_from_headlines(headlines: list[str]) -> tuple[float, float, list[str]]:
    # Very simple bag-of-words placeholder classifier
    score = 0.0
    confidence = 0.0
    drivers: list[str] = []
    for h in headlines:
        lh = h.lower()
        if "gold" in lh and ("rally" in lh or "surge" in lh or "bull" in lh):
            score += 0.3; confidence += 0.2; drivers.append(h)
        if "gold" in lh and ("drop" in lh or "sell" in lh or "bear" in lh or "fall" in lh):
            score -= 0.3; confidence += 0.2; drivers.append(h)
    score = max(-1.0, min(1.0, score))
    confidence = min(1.0, confidence)
    return score, confidence, drivers

def main() -> None:
    api_key = os.getenv("FINNHUB_API_KEY", "")
    headlines: list[str] = []
    error = None
    try:
        if api_key:
            data = fetch_finnhub_gold_sentiment(api_key)
            headlines = data.get("headlines", []) if isinstance(data, dict) else []
        else:
            error = "no FINNHUB_API_KEY"
    except Exception as exc:
        error = repr(exc)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if error:
        OUT.write_text(json.dumps({"state": "unknown", "source": "finnhub", "updated_at": now(), "fresh": False, "error": error}, indent=2), encoding="utf-8")
        print(json.dumps({"error": error}, indent=2))
        return

    score, confidence, drivers = classify_from_headlines(headlines)
    state = "bullish" if score > 0.15 else "bearish" if score < -0.15 else "neutral"
    payload = {
        "state": state,
        "source": "finnhub",
        "updated_at": now(),
        "fresh": True,
        "score": score,
        "confidence": confidence,
        "drivers": drivers,
        "headlines": headlines,
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))

if __name__ == "__main__":
    main()
