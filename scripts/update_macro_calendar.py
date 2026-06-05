#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "macro" / "economic_calendar.json"
MANUAL_PATH = ROOT / "config" / "manual_macro_calendar.json"

def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def _http_get_json(url: str, headers: dict | None = None, timeout: int = 10) -> object:
    req = urllib.request.Request(url)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def _normalize_event(ev: dict) -> dict:
    # Normalize various provider event shapes into keys the engine expects
    impact = ev.get("impact") or ev.get("importance") or ev.get("significance") or ev.get("Impact") or ""
    currency = (ev.get("currency") or ev.get("country") or ev.get("Country") or "").upper()
    name = ev.get("event") or ev.get("title") or ev.get("name") or ev.get("Event") or "macro event"
    # time candidates
    time_utc = ev.get("time_utc") or ev.get("datetime") or ev.get("date") or ev.get("time") or ev.get("Date")
    return {"impact": impact, "currency": currency, "name": name, "time_utc": time_utc}

def fetch_tradingeconomics_calendar(client: str, secret: str) -> list[dict]:
    # Try a few common TradingEconomics patterns; treat any successful JSON list as candidate
    urls = [
        f"https://api.tradingeconomics.com/calendar?c={client}:{secret}",
        f"https://api.tradingeconomics.com/calendar?client={client}&key={secret}",
        f"https://api.tradingeconomics.com/calendar?c={client}:{secret}&d1={(datetime.utcnow()-timedelta(days=1)).strftime('%Y-%m-%d')}&d2={(datetime.utcnow()+timedelta(days=1)).strftime('%Y-%m-%d')}",
    ]
    last_exc = None
    for url in urls:
        try:
            data = _http_get_json(url)
            if isinstance(data, list):
                return [_normalize_event(dict(ev)) for ev in data]
            # Some responses may wrap events in a field
            if isinstance(data, dict):
                for key in ("data", "calendar", "events"):
                    if isinstance(data.get(key), list):
                        return [_normalize_event(dict(ev)) for ev in data.get(key)]
            last_exc = RuntimeError(f"unexpected tradingeconomics response shape from {url}")
        except Exception as exc:
            last_exc = exc
            continue
    raise last_exc or RuntimeError("tradingeconomics fetch failed")

def fetch_finnhub_calendar(api_key: str) -> list[dict]:
    now_dt = datetime.utcnow()
    frm = (now_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    to = (now_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"https://finnhub.io/api/v1/calendar/economic?from={frm}&to={to}&token={api_key}"
    data = _http_get_json(url)
    # finnub may return dict wrappers
    if isinstance(data, dict):
        for key in ("data", "economicCalendar", "calendar", "events"):
            if isinstance(data.get(key), list):
                return [_normalize_event(dict(ev)) for ev in data.get(key)]
        # sometimes the top-level is list-like
        if any(isinstance(v, list) for v in data.values()):
            for v in data.values():
                if isinstance(v, list):
                    return [_normalize_event(dict(ev)) for ev in v]
    if isinstance(data, list):
        return [_normalize_event(dict(ev)) for ev in data]
    raise RuntimeError("unexpected finnhub economic calendar response")

def fetch_fmp_stable(api_key: str) -> list[dict]:
    now_dt = datetime.utcnow()
    frm = (now_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    to = (now_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"https://financialmodelingprep.com/stable/economic-calendar?from={frm}&to={to}&apikey={api_key}"
    data = _http_get_json(url)
    if isinstance(data, list):
        return [_normalize_event(dict(ev)) for ev in data]
    if isinstance(data, dict):
        for key in ("data", "events", "calendar"):
            if isinstance(data.get(key), list):
                return [_normalize_event(dict(ev)) for ev in data.get(key)]
    raise RuntimeError("unexpected fmp economic calendar response")

def read_manual_calendar(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # If top-level contains events key
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return [_normalize_event(dict(ev)) for ev in data.get("events")]
        # If file itself is a list
        if isinstance(data, list):
            return [_normalize_event(dict(ev)) for ev in data]
    except Exception:
        return []
    return []

def build_state(events: list[dict], source: str) -> dict:
    state = "clear"
    next_event = None
    block_now = False
    now_dt = datetime.now(timezone.utc)
    for ev in events:
        impact = str(ev.get("impact") or ev.get("importance") or "").lower()
        if "high" in impact:
            et = ev.get("time_utc") or ev.get("datetime") or ev.get("time")
            next_event = ev
            block_now = True
            state = "blocked"
            break
    # Consider provider success as fresh even if no events were returned
    return {
        "state": state,
        "source": source,
        "updated_at": now(),
        "fresh": True,
        "next_event": next_event,
        "block_now": block_now,
        "events": events,
    }

def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    events: list[dict] = []
    source = "none"

    # 1) TradingEconomics
    te_client = os.getenv("TRADING_ECONOMICS_CLIENT", "").strip()
    te_secret = os.getenv("TRADING_ECONOMICS_SECRET", "").strip()
    if te_client and te_secret:
        try:
            events = fetch_tradingeconomics_calendar(te_client, te_secret)
            source = "tradingeconomics"
        except Exception as exc:
            errors.append(f"tradingeconomics: {repr(exc)}")

    # 2) Finnhub economic calendar
    if source == "none":
        fh = os.getenv("FINNHUB_API_KEY", "").strip()
        if fh:
            try:
                events = fetch_finnhub_calendar(fh)
                source = "finnhub"
            except Exception as exc:
                errors.append(f"finnhub: {repr(exc)}")

    # 3) FMP stable economic-calendar
    if source == "none":
        fmp = os.getenv("FMP_API_KEY", "").strip()
        if fmp:
            try:
                events = fetch_fmp_stable(fmp)
                source = "fmp"
            except Exception as exc:
                errors.append(f"fmp: {repr(exc)}")

    # 4) Manual local file fallback
    if source == "none":
        manual_events = read_manual_calendar(MANUAL_PATH)
        if manual_events:
            events = manual_events
            source = "manual"
        else:
            errors.append("manual: no manual file or empty")

    # If still none, write unknown state
    if source == "none":
        OUT.write_text(json.dumps({
            "state": "unknown",
            "fresh": False,
            "source": "none",
            "error": "no macro provider available",
            "events": []
        }, indent=2), encoding="utf-8")
        print(json.dumps({"error": "no macro provider available", "details": errors}, indent=2))
        return

    # Build normalized state and write it
    state = build_state(events, source)
    OUT.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(json.dumps(state, indent=2))

if __name__ == "__main__":
    main()
