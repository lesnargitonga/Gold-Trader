"""Twelve Data time-series client for cloud / Render candle loading."""
from __future__ import annotations

import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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


def _timeframe_minutes(timeframe: str) -> int | None:
    key = timeframe.strip().upper()
    fixed = {
        "D1": 1440,
        "1D": 1440,
        "H4": 240,
        "4H": 240,
        "H1": 60,
        "1H": 60,
        "M30": 30,
        "30M": 30,
        "M15": 15,
        "15M": 15,
        "M5": 5,
        "5M": 5,
        "M1": 1,
        "1M": 1,
    }
    if key in fixed:
        return fixed[key]
    if key.endswith("M") and key[:-1].isdigit():
        return int(key[:-1])
    if key.endswith("H") and key[:-1].isdigit():
        return int(key[:-1]) * 60
    return None


def _csv_rows_from_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    try:
        with path.open("rb") as f:
            header = f.readline().decode("utf-8", errors="replace").strip()
            if not header:
                return []
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            data = b""
            target_lines = limit + 20
            while size > 0 and data.count(b"\n") <= target_lines:
                read_size = min(block, size)
                size -= read_size
                f.seek(size)
                data = f.read(read_size) + data
        lines = [
            line for line in data.decode("utf-8", errors="replace").splitlines()
            if line and line.strip() != header
        ][-target_lines:]
        rows: list[dict[str, Any]] = []
        for row in csv.DictReader([header, *lines]):
            try:
                rows.append(
                    {
                        "time": str(row.get("datetime") or row.get("timestamp") or row.get("time") or ""),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row.get("volume") or 0),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        if len(rows) >= 2 and rows[0]["time"] > rows[-1]["time"]:
            rows.reverse()
        return rows[-limit:]
    except OSError:
        return []


def _csv_chart_fallback(timeframe: str, count: int, repo: Path) -> tuple[list[dict[str, Any]], str | None]:
    minutes = _timeframe_minutes(timeframe)
    if minutes is None:
        return [], None
    data_dir = repo / "data"
    names = [
        data_dir / "live_xauusd" / f"xauusd_{minutes}m.csv",
        data_dir / "agent_live_xauusd" / f"xauusd_{minutes}m.csv",
        data_dir / "live_tracker" / f"xauusd_{minutes}m.csv",
        data_dir / "current_xauusd" / f"xauusd_{minutes}m.csv",
        data_dir / "xauusd_5y" / f"xauusd_5y_{minutes}m.csv",
        data_dir / f"xauusd_full_{minutes}m.csv",
    ]
    candidates: list[Path] = []
    for path in names:
        if path not in candidates:
            candidates.append(path)
    if data_dir.exists():
        try:
            for path in data_dir.rglob(f"xauusd*_{minutes}m.csv"):
                if path not in candidates:
                    candidates.append(path)
        except OSError:
            pass
    existing = sorted(
        [path for path in candidates if path.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in existing:
        rows = _csv_rows_from_tail(path, count)
        if rows:
            try:
                rel = str(path.relative_to(repo))
            except ValueError:
                rel = str(path)
            return rows, rel
    return [], None


def _yahoo_params(timeframe: str) -> tuple[str, str, int] | None:
    key = timeframe.strip().upper()
    params = {
        "D1": ("1d", "1y", 1),
        "1D": ("1d", "1y", 1),
        "H4": ("60m", "3mo", 4),
        "4H": ("60m", "3mo", 4),
        "H1": ("60m", "1mo", 1),
        "1H": ("60m", "1mo", 1),
        "M30": ("30m", "1mo", 1),
        "30M": ("30m", "1mo", 1),
        "M15": ("15m", "5d", 1),
        "15M": ("15m", "5d", 1),
        "M5": ("5m", "5d", 1),
        "5M": ("5m", "5d", 1),
        "M1": ("1m", "1d", 1),
        "1M": ("1m", "1d", 1),
    }
    return params.get(key)


def _aggregate_rows(rows: list[dict[str, Any]], group_size: int) -> list[dict[str, Any]]:
    if group_size <= 1:
        return rows
    out: list[dict[str, Any]] = []
    for i in range(0, len(rows), group_size):
        group = rows[i:i + group_size]
        if not group:
            continue
        out.append(
            {
                "time": group[0]["time"],
                "open": group[0]["open"],
                "high": max(r["high"] for r in group),
                "low": min(r["low"] for r in group),
                "close": group[-1]["close"],
                "volume": sum(float(r.get("volume") or 0) for r in group),
            }
        )
    return out


def _fetch_yahoo_chart_rows(timeframe: str, count: int) -> tuple[list[dict[str, Any]], str | None, str]:
    enabled = os.getenv("GOLD_ENABLE_YAHOO_CHART_FALLBACK", "true").lower() not in {"0", "false", "no"}
    symbol = os.getenv("GOLD_YAHOO_FALLBACK_SYMBOL", "GC=F")
    if not enabled:
        return [], "Yahoo chart fallback disabled", symbol
    params = _yahoo_params(timeframe)
    if not params:
        return [], f"Yahoo chart fallback does not support {timeframe}", symbol
    interval, range_, group_size = params
    query = urllib.parse.urlencode({"interval": interval, "range": range_})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "gold-trader/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        return [], str(exc), symbol
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict):
        return [], "Yahoo chart response missing chart payload", symbol
    if chart.get("error"):
        return [], str(chart["error"]), symbol
    result = (chart.get("result") or [None])[0]
    if not isinstance(result, dict):
        return [], "Yahoo chart response missing result", symbol
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    if not timestamps or not isinstance(quote, dict):
        return [], "Yahoo chart response missing OHLC data", symbol
    rows: list[dict[str, Any]] = []
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    for i, ts in enumerate(timestamps):
        try:
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        except IndexError:
            continue
        if o is None or h is None or l is None or c is None:
            continue
        try:
            rows.append(
                {
                    "time": datetime.fromtimestamp(float(ts), timezone.utc).isoformat(),
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                    "volume": float(volumes[i] or 0) if i < len(volumes) else 0.0,
                }
            )
        except (TypeError, ValueError, OSError):
            continue
    rows = _aggregate_rows(rows, group_size)
    return rows[-count:], None if rows else "Yahoo chart returned no usable candles", symbol


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
    count = max(1, min(int(count), 500))
    chart_ttl = int(os.environ.get("GOLD_CHART_CACHE_SECONDS", "600"))
    rows, err, stale = _fetch_twelvedata_rows(
        symbol, tf, limit=count, repo=repo, cache_ttl_seconds=chart_ttl
    )
    root = repo or Path(__file__).resolve().parents[3]
    provider = "twelvedata"
    fallback_note: str | None = None
    provider_error = err
    if not rows:
        yahoo_rows, yahoo_error, yahoo_symbol = _fetch_yahoo_chart_rows(tf, count)
        if yahoo_rows:
            rows = yahoo_rows
            provider = "yahoo_gc_futures"
            fallback_note = (
                f"Showing Yahoo {yahoo_symbol} futures candles because Twelve Data is unavailable."
            )
        else:
            csv_rows, csv_path = _csv_chart_fallback(tf, count, root)
            if csv_rows:
                rows = csv_rows
                provider = "csv_fallback"
                fallback_note = f"Showing local cached CSV candles from {csv_path}."
            elif yahoo_error and provider_error:
                provider_error = f"{provider_error}; Yahoo fallback: {yahoo_error}"
            elif yahoo_error:
                provider_error = yahoo_error
    payload_symbol = symbol
    if provider == "yahoo_gc_futures":
        payload_symbol = os.getenv("GOLD_YAHOO_FALLBACK_SYMBOL", "GC=F")
    payload: dict[str, Any] = {
        "ok": bool(rows),
        "tf": tf,
        "provider": provider,
        "symbol": payload_symbol,
        "candles": rows,
        "count": len(rows),
        "volume_note": "XAU/USD volume may be 0.0 on this feed.",
    }
    if stale and provider == "twelvedata":
        payload["cache_note"] = "Showing cached candles (live feed was rate-limited or unavailable)."
    if fallback_note:
        payload["fallback_note"] = fallback_note
    if provider_error and provider != "twelvedata":
        payload["primary_error"] = provider_error
    elif provider_error and not rows:
        payload["error"] = provider_error
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
