"""Tests for Twelve Data candle client."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from gold_trader.data.twelvedata import (
    candles_for_chart,
    fetch_twelvedata_candles,
    interval_for_timeframe,
    normalize_twelve_data_symbol,
)


def test_normalize_symbol() -> None:
    assert normalize_twelve_data_symbol("XAUUSD") == "XAU/USD"
    assert normalize_twelve_data_symbol("GOLD") == "XAU/USD"


def test_interval_mapping() -> None:
    assert interval_for_timeframe("H4") == "4h"
    assert interval_for_timeframe("M15") == "15min"


def test_fetch_parses_response(tmp_path: Path) -> None:
    payload = {
        "status": "ok",
        "values": [
            {
                "datetime": "2026-06-04 10:00:00",
                "open": "3300",
                "high": "3310",
                "low": "3295",
                "close": "3305",
                "volume": "100",
            },
            {
                "datetime": "2026-06-04 11:00:00",
                "open": "3305",
                "high": "3315",
                "low": "3300",
                "close": "3310",
                "volume": "120",
            },
        ],
    }

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

    with patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "test-key"}, clear=False):
        with patch("urllib.request.urlopen", return_value=FakeResp()):
            rows = fetch_twelvedata_candles("XAUUSD", "H1", limit=10, repo=tmp_path, use_cache=False)
    assert len(rows) == 2
    assert rows[0]["close"] == 3305.0
    assert rows[1]["time"].startswith("2026-06-04")


def test_candles_for_chart_reports_error_when_empty(tmp_path: Path) -> None:
    payload = {"status": "error", "message": "API credits exceeded"}

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

    with patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "test-key"}, clear=False):
        with patch("urllib.request.urlopen", return_value=FakeResp()):
            out = candles_for_chart("H4", symbol="XAUUSD", count=50, repo=tmp_path)
    assert out["ok"] is False
    assert out["count"] == 0
    assert "credit" in out["error"].lower() or "credit" in out["error"].lower()


def test_candles_for_chart_uses_stale_cache_on_api_error(tmp_path: Path) -> None:
    from gold_trader.data import twelvedata as td

    cache = td._cache_path(tmp_path, "XAU/USD", "4h")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "fetched_at": 1.0,
                "values": [
                    {
                        "datetime": "2026-06-04 10:00:00",
                        "open": "3300",
                        "high": "3310",
                        "low": "3295",
                        "close": "3305",
                        "volume": "0",
                    }
                ],
            }
        )
    )
    payload = {"status": "error", "message": "API credits exceeded"}

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

    with patch.dict("os.environ", {"TWELVE_DATA_API_KEY": "test-key"}, clear=False):
        with patch("urllib.request.urlopen", return_value=FakeResp()):
            out = candles_for_chart("H4", symbol="XAUUSD", count=50, repo=tmp_path)
    assert out["ok"] is True
    assert out["count"] == 1
    assert out.get("cache_note")
