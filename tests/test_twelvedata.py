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

    with patch.dict(
        "os.environ",
        {"TWELVE_DATA_API_KEY": "test-key", "GOLD_ENABLE_YAHOO_CHART_FALLBACK": "false"},
        clear=False,
    ):
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


def test_candles_for_chart_uses_csv_fallback_on_api_error(tmp_path: Path) -> None:
    csv_dir = tmp_path / "data" / "live_xauusd"
    csv_dir.mkdir(parents=True)
    (csv_dir / "xauusd_15m.csv").write_text(
        "\n".join(
            [
                "timestamp,open,high,low,close,volume",
                "2026-06-04T10:00:00+00:00,3300,3310,3295,3305,10",
                "2026-06-04T10:15:00+00:00,3305,3312,3301,3308,11",
            ]
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

    with patch.dict(
        "os.environ",
        {"TWELVE_DATA_API_KEY": "test-key", "GOLD_ENABLE_YAHOO_CHART_FALLBACK": "false"},
        clear=False,
    ):
        with patch("urllib.request.urlopen", return_value=FakeResp()):
            out = candles_for_chart("M15", symbol="XAUUSD", count=50, repo=tmp_path)
    assert out["ok"] is True
    assert out["provider"] == "csv_fallback"
    assert out["count"] == 2
    assert out["candles"][-1]["close"] == 3308.0


def test_candles_for_chart_honors_small_count_with_csv_fallback(tmp_path: Path) -> None:
    csv_dir = tmp_path / "data" / "live_xauusd"
    csv_dir.mkdir(parents=True)
    (csv_dir / "xauusd_15m.csv").write_text(
        "\n".join(
            [
                "timestamp,open,high,low,close,volume",
                "2026-06-04T10:00:00+00:00,3300,3310,3295,3305,10",
                "2026-06-04T10:15:00+00:00,3305,3312,3301,3308,11",
                "2026-06-04T10:30:00+00:00,3308,3316,3303,3314,12",
            ]
        )
    )

    class FakeResp:
        def read(self) -> bytes:
            return json.dumps({"status": "error", "message": "API credits exceeded"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

    with patch.dict(
        "os.environ",
        {"TWELVE_DATA_API_KEY": "test-key", "GOLD_ENABLE_YAHOO_CHART_FALLBACK": "false"},
        clear=False,
    ):
        with patch("urllib.request.urlopen", return_value=FakeResp()):
            out = candles_for_chart("M15", symbol="XAUUSD", count=1, repo=tmp_path)
    assert out["count"] == 1
    assert len(out["candles"]) == 1
    assert out["candles"][0]["close"] == 3314.0


def test_candles_for_chart_caches_fallback_payload(tmp_path: Path) -> None:
    yahoo_rows = [
        {"time": "2026-06-04T10:00:00+00:00", "open": 3300, "high": 3310, "low": 3295, "close": 3305, "volume": 10},
        {"time": "2026-06-04T10:15:00+00:00", "open": 3305, "high": 3312, "low": 3301, "close": 3308, "volume": 11},
    ]
    with patch.dict("os.environ", {"GOLD_CHART_FALLBACK_CACHE_SECONDS": "300"}, clear=False):
        with (
            patch("gold_trader.data.twelvedata._fetch_twelvedata_rows", return_value=([], "rate limited", False)),
            patch("gold_trader.data.twelvedata._fetch_yahoo_chart_rows", return_value=(yahoo_rows, None, "GC=F")),
        ):
            first = candles_for_chart("M15", symbol="XAUUSD", count=2, repo=tmp_path)
        with (
            patch("gold_trader.data.twelvedata._fetch_twelvedata_rows", side_effect=AssertionError("Twelve Data should be cached")),
            patch("gold_trader.data.twelvedata._fetch_yahoo_chart_rows", side_effect=AssertionError("Yahoo should be cached")),
        ):
            second = candles_for_chart("M15", symbol="XAUUSD", count=1, repo=tmp_path)
    assert first["provider"] == "yahoo_gc_futures"
    assert second["provider"] == "yahoo_gc_futures"
    assert second["count"] == 1
    assert second["candles"][0]["close"] == 3308
    assert "cached" in second["cache_note"].lower()
