"""Twelve Data time-series client for cloud / Render candle loading."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..infra.secrets import resolve_twelve_data_api_key as _resolve_key

API_BASE = "https://api.twelvedata.com/time_series"

# Gold Trader timeframe labels -> Twelve Data interval strings
TF_TO_INTERVAL: dict[str, str] = {
    "D1": "1day",
    "H4": "4h",
    "H1": "1h",
    "M30": "30min",
    "M15": "15min",
    "M5": "5min",
    "M1": "1min",
    "1D": "1day",
    "4H": "4h",
    "1H": "1h",
    "30M": "30min",
    "15M": "15min",
    "5M": "5min",
    "1M": "1min",
}


def twelvedata_configured(*, path: Path | None = None) -> bool:
    return bool(_resolve_key(path=path))


def normalize_twelve_data_symbol(symbol: str) -> str:
    override = os.environ.get("GOLD_TWELVE_DATA_SYMBOL", "").strip()
    if override:
        return override
    s = symbol.upper().replace(" ", "")
    if s in {"XAUUSD", "GOLD", "XAU"}:
        return "XAU/USD"
    if "/" in symbol:
        return symbol
    if len(s) == 6 and s.isalpha():
        return f"{s[:3]}/{s[3:]}"
    return symbol


def interval_for_timeframe(timeframe: str) -> str | None:
    key = timeframe.strip().upper()
    if key in TF_TO_INTERVAL:
        return TF_TO_INTERVAL[key]
    if key.endswith("M") and key[:-1].isdigit():
        return f"{key[:-1]}min"
    if key.endswith("H") and key[:-1].isdigit():
        return f"{key[:-1]}h"
    return None


def _cache_path(repo: Path, symbol: str, interval: str) -> Path:
    safe = symbol.replace("/", "_").lower()
    return repo / "data" / "cache" / "twelvedata" / f"{safe}_{interval}.json"


def _read_cache(path: Path, ttl_seconds: int) -> list[dict[str, Any]] | None:
    if not path.exists() or ttl_seconds <= 0:
        return None
    try:
        payload = json.loads(path.read_text())
        if time.time() - float(payload.get("fetched_at", 0)) > ttl_seconds:
            return None
        rows = payload.get("values")
        return rows if isinstance(rows, list) else None
    except Exception:
        return None


def _write_cache(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fetched_at": time.time(), "values": rows}, indent=0),
    )


def fetch_twelvedata_candles(
    symbol: str,
    timeframe: str,
    *,
    limit: int = 500,
    repo: Path | None = None,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """Return OHLC rows oldest-first: time, open, high, low, close, volume."""
    apikey = _resolve_key()
    if not apikey:
        return []
    interval = interval_for_timeframe(timeframe)
    if not interval:
        return []
    td_symbol = normalize_twelve_data_symbol(symbol)
    root = repo or Path(__file__).resolve().parents[3]
    ttl = int(os.environ.get("GOLD_TWELVE_DATA_CACHE_SECONDS", "120"))
    cache = _cache_path(root, td_symbol, interval)
    if use_cache:
        cached = _read_cache(cache, ttl)
        if cached:
            return _normalize_rows(cached)[-limit:]

    params = {
        "symbol": td_symbol,
        "interval": interval,
        "outputsize": str(min(max(limit, 30), 5000)),
        "order": "ASC",
        "timezone": "UTC",
        "apikey": apikey,
    }
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return []

    if not isinstance(payload, dict):
        return []
    status = str(payload.get("status") or "").lower()
    if status and status not in ("ok", "success"):
        return []
    values = payload.get("values")
    if not isinstance(values, list):
        return []
    rows = _normalize_rows(values)
    if rows and use_cache:
        _write_cache(cache, values)
    return rows[-limit:]


def _normalize_rows(values: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in values:
        if not isinstance(row, dict):
            continue
        try:
            out.append(
                {
                    "time": str(row.get("datetime") or row.get("time") or ""),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume") or 0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    if len(out) >= 2 and out[0]["time"] > out[-1]["time"]:
        out.reverse()
    return out
