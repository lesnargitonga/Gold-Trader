"""Tests for Twelve Data candle client."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from gold_trader.data.twelvedata import (
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
