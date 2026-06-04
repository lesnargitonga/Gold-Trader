"""Smoke tests for the Render market-intelligence command center."""
from __future__ import annotations

import json
import threading
import time
import urllib.request
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gold_trader.core.market_intelligence_ux import provider_health
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

    def test_provider_health_exposes_missing_feed_contracts(self) -> None:
        decision = {
            "timestamp_utc": "2026-06-04T10:00:00+00:00",
            "cloud_status": {"data_provider": "twelvedata", "candles_loaded": 0},
            "timeframe_reads": [
                {"timeframe": "M15", "candles": 0, "warnings": ["Twelve Data unavailable for M15: HTTP Error 429"]},
            ],
            "market_context": {},
        }
        with patch.dict(os.environ, {}, clear=True):
            health = provider_health(decision, {})
        assert health["twelvedata"]["state"] == "degraded"
        assert "429" in health["twelvedata"]["message"]
        assert health["chart_fallback"]["state"] == "chart_only"
        assert health["chart_fallback"]["severity"] == "warning"
        assert health["cme"]["state"] == "missing_credentials"
        assert health["cme"]["required_env"]
        assert health["options"]["state"] in {"manual_proxy", "missing_credentials"}

    def test_provider_health_surfaces_upstream_errors_and_proxy_warning(self) -> None:
        decision = {
            "timestamp_utc": "2026-06-04T10:00:00+00:00",
            "cloud_status": {"data_provider": "twelvedata", "candles_loaded": 50},
            "timeframe_reads": [{"timeframe": "M15", "candles": 50}],
            "market_context": {"cross_market_state": "available", "cross_market_source": "twelvedata_quote"},
        }
        context = {
            "macro_state": {"state": "unknown", "source": "fmp", "error": "<HTTPError 403: 'Forbidden'>"},
            "spread_state": {"state": "unknown_nonfatal_in_paper", "source": "twelvedata", "error": "<HTTPError 429: 'Too Many Requests'>"},
            "volatility_state": {"state": "normal", "source": "twelvedata_M15", "atr": 4.1},
        }
        with patch.dict(os.environ, {}, clear=True):
            health = provider_health(decision, context)
        assert health["fmp_macro"]["source"] == "fmp"
        assert "403" in health["fmp_macro"]["message"]
        assert "429" in health["spread"]["message"]
        assert health["cross_market"]["state"] == "available"
        assert health["volatility"]["state"] == "normal"
        assert health["options"]["severity"] == "warning"

    def test_provider_health_endpoint_normalizes_stale_dict_state(self) -> None:
        stale = {"cot": {"state": {"state": "unknown", "source": "not_connected"}, "label": "COT"}}
        with (
            patch.object(mi, "get_decision_for_api", side_effect=RuntimeError("boom")),
            patch.object(mi, "read_json", return_value=stale),
        ):
            status, _ctype, body = self.get("/api/provider-health")
        payload = json.loads(body)
        assert status == 200
        assert payload["cot"]["state"] == "unknown"
        assert payload["cot"]["source"] == "not_connected"

    def test_decision_endpoint_recomputes_fresh_provider_health(self) -> None:
        decision = {
            "timestamp_utc": "2026-06-04T10:00:00+00:00",
            "symbol": "XAUUSD",
            "current_price": 4501,
            "cloud_status": {"data_provider": "twelvedata", "candles_loaded": 20, "execution_mode": "paper"},
            "timeframe_reads": [{"timeframe": "M15", "candles": 20}],
            "provider_health_summary": {
                "chart_fallback": {"state": "available", "severity": "ok", "label": "old"},
            },
            "live_market_context": {
                "macro_state": {"state": "unknown", "source": "fmp", "error": "<HTTPError 403: 'Forbidden'>"},
                "spread_state": {
                    "state": "unknown_nonfatal_in_paper",
                    "source": "twelvedata",
                    "error": "<HTTPError 429: 'Too Many Requests'>",
                },
                "volatility_state": {"state": "normal", "source": "twelvedata_M15"},
            },
        }
        with patch.dict(os.environ, {}, clear=True), patch.object(mi, "get_decision_for_api", return_value=decision):
            status, _ctype, body = self.get("/api/decision")
        payload = json.loads(body)
        health = payload["provider_health_summary"]
        assert status == 200
        assert health["chart_fallback"]["state"] == "chart_only"
        assert health["chart_fallback"]["severity"] == "warning"
        assert "403" in health["fmp_macro"]["message"]
        assert "429" in health["spread"]["message"]
        assert payload["market_intelligence_summary"]["chart"] == "chart_only"
        assert payload["market_levels_summary"]["state"] in {"manual_proxy", "missing"}

    def test_market_levels_endpoint_returns_manual_proxy_levels(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "market_levels.json").write_text(
            json.dumps(
                {
                    "description": "manual",
                    "levels": [
                        {"price": 4400, "kind": "put_wall", "label": "4400 put wall", "strength": "2"},
                        {"price": 4500, "kind": "round", "label": "4500 round"},
                    ],
                }
            )
        )
        decision = {"current_price": 4498, "cloud_status": {"execution_mode": "paper"}}
        with patch.object(mi, "ROOT", tmp_path), patch.object(mi, "get_decision_for_api", return_value=decision):
            status, _ctype, body = self.get("/api/market-levels")
        payload = json.loads(body)
        assert status == 200
        assert payload["state"] == "manual_proxy"
        assert payload["levels"][0]["price"] == 4500
        assert payload["levels"][0]["distance_points"] == 2
        assert payload["levels"][1]["strength"] == 2
