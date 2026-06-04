"""Smoke tests for the Render market-intelligence command center."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gold_trader.web import market_intelligence_api as mi


class TestMarketIntelligenceApi:
    def setup_method(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), mi.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.05)

    def teardown_method(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def get(self, path: str) -> tuple[int, str, str]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8")

    def test_command_center_asset_uses_embedded_fallback_when_file_missing(self) -> None:
        with patch.object(mi, "_command_center_js_candidates", return_value=[Path("/tmp/no-such-command-center.js")]):
            status, ctype, body = self.get("/command-center.js")
        assert status == 200
        assert "application/javascript" in ctype
        assert "/api/decision" in body

    def test_static_app_js_alias_serves_command_center(self) -> None:
        status, ctype, body = self.get("/static/app.js")
        assert status == 200
        assert "application/javascript" in ctype
        assert "Gold Trader" in body or "/api/decision" in body

    def test_summary_compatibility_endpoint(self) -> None:
        with patch.object(mi, "get_decision_for_api", return_value={"symbol": "XAUUSD", "cloud_status": {"execution_mode": "paper"}}):
            status, ctype, body = self.get("/api/summary")
        assert status == 200
        assert "json" in ctype
        payload = json.loads(body)
        assert payload["config"]["symbol"] == "XAUUSD"
        assert "decision" in payload

    def test_live_candles_compatibility_endpoint(self) -> None:
        fake = {
            "provider": "csv_fallback",
            "symbol": "XAUUSD",
            "candles": [{"time": "2026-06-04T10:00:00+00:00", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 0}],
            "count": 1,
            "fallback_note": "fallback",
        }
        with patch.object(mi, "_candles", return_value=fake):
            status, _ctype, body = self.get("/api/live/candles?timeframe=15&count=1")
        payload = json.loads(body)
        assert status == 200
        assert payload["source"] == "csv_fallback"
        assert payload["count"] == 1
        assert payload["bars"][0]["close"] == 2
