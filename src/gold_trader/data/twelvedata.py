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
    if not path.exists():
        return None
    if ttl_seconds <= 0:
        return _read_cache_stale(path)
    try:
        payload = json.loads(path.read_text())
        if time.time() - float(payload.get("fetched_at", 0)) > ttl_seconds:
            return None
        rows = payload.get("values")
        return rows if isinstance(rows, list) else None
    except Exception:
        return None


def _read_cache_stale(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        rows = payload.get("values")
        return rows if isinstance(rows, list) else None
    except Exception:
        return None


def _api_error_message(payload: dict[str, Any]) -> str:
    return str(payload.get("message") or payload.get("code") or "Twelve Data request failed")


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
    rows, _err, _stale = _fetch_twelvedata_rows(
        symbol, timeframe, limit=limit, repo=repo, use_cache=use_cache
    )
    return rows


def _fetch_twelvedata_rows(
    symbol: str,
    timeframe: str,
    *,
    limit: int = 500,
    repo: Path | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: int | None = None,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Return (rows, error, served_from_stale_cache)."""
    apikey = _resolve_key()
    if not apikey:
        return [], "Twelve Data API key not configured", False
    interval = interval_for_timeframe(timeframe)
    if not interval:
        return [], f"unsupported timeframe {timeframe}", False
    td_symbol = normalize_twelve_data_symbol(symbol)
    root = repo or Path(__file__).resolve().parents[3]
    ttl = cache_ttl_seconds if cache_ttl_seconds is not None else int(
        os.environ.get("GOLD_TWELVE_DATA_CACHE_SECONDS", "120")
    )
    cache = _cache_path(root, td_symbol, interval)

    if use_cache:
        cached = _read_cache(cache, ttl)
        if cached:
            rows = _normalize_rows(cached)[-limit:]
            if rows:
                return rows, None, False

    params = {
        "symbol": td_symbol,
        "interval": interval,
        "outputsize": str(min(max(limit, 30), 5000)),
        "order": "ASC",
        "timezone": "UTC",
        "apikey": apikey,
    }
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    last_error: str | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(url, timeout=25) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            payload = None

        if not isinstance(payload, dict):
            if attempt == 0:
                time.sleep(1.0)
                continue
            break

        status = str(payload.get("status") or "").lower()
        if status and status not in ("ok", "success"):
            last_error = _api_error_message(payload)
            if attempt == 0 and any(x in last_error.lower() for x in ("credit", "limit", "rate", "minute")):
                time.sleep(1.2)
                continue
            break

        values = payload.get("values")
        if not isinstance(values, list):
            last_error = "Twelve Data response missing candle values"
            break
        rows = _normalize_rows(values)
        if rows and use_cache:
            _write_cache(cache, values)
        if rows:
            return rows[-limit:], None, False
        last_error = "Twelve Data returned no usable candles"
        break

    stale = _read_cache_stale(cache) if use_cache else None
    if stale:
        rows = _normalize_rows(stale)[-limit:]
        if rows:
            return rows, None, True
    return [], last_error or "Unable to load candles", False


def candles_for_chart(
    timeframe: str,
    *,
    symbol: str = "XAUUSD",
    count: int = 280,
    repo: Path | None = None,
) -> dict[str, Any]:
    """Payload for /api/candles — never reports ok when count is zero."""
    tf = timeframe.strip().upper()
    count = max(20, min(int(count), 500))
    chart_ttl = int(os.environ.get("GOLD_CHART_CACHE_SECONDS", "600"))
    rows, err, stale = _fetch_twelvedata_rows(
        symbol, tf, limit=count, repo=repo, cache_ttl_seconds=chart_ttl
    )
    payload: dict[str, Any] = {
        "ok": bool(rows),
        "tf": tf,
        "provider": "twelvedata",
        "candles": rows,
        "count": len(rows),
        "volume_note": "XAU/USD volume may be 0.0 on this feed.",
    }
    if stale:
        payload["cache_note"] = "Showing cached candles (live feed was rate-limited or unavailable)."
    if err and not rows:
        payload["error"] = err
    return payload


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


def fetch_candles(
    timeframe: str,
    *,
    symbol: str = "XAUUSD",
    count: int = 500,
    repo: Path | None = None,
) -> list[dict[str, Any]]:
    """UI-friendly alias (timeframe-first) used by absolute_gold_app."""
    return fetch_twelvedata_candles(symbol, timeframe, limit=count, repo=repo)
