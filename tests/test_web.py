"""Smoke tests for the gold_trader.web HTTP server."""
from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from gold_trader.web import build_server
from gold_trader.web.runtime_config import (
    RuntimeConfig, load_runtime_config, save_runtime_config,
)


class RuntimeConfigTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "cfg.json"
            cfg = RuntimeConfig(macro_filter_mode="hard", auto_trade_enabled=False, notes="hi")
            save_runtime_config(cfg, p)
            loaded = load_runtime_config(p)
            self.assertEqual(loaded.macro_filter_mode, "hard")
            self.assertFalse(loaded.auto_trade_enabled)
            self.assertEqual(loaded.notes, "hi")

    def test_defaults_on_missing(self) -> None:
        cfg = load_runtime_config(Path("/tmp/__nonexistent__.json"))
        self.assertEqual(cfg.macro_filter_mode, "soft")
        self.assertTrue(cfg.auto_trade_enabled)

    def test_invalid_mode_falls_back(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "cfg.json"
            p.write_text(json.dumps({"macro_filter_mode": "bogus"}))
            cfg = load_runtime_config(p)
            self.assertEqual(cfg.macro_filter_mode, "soft")


class WebServerSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = build_server(host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.05)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _get(self, path: str) -> tuple[int, dict | str]:
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as r:
            body = r.read().decode("utf-8")
            ctype = r.headers.get("Content-Type", "")
            if "json" in ctype:
                return r.status, json.loads(body)
            return r.status, body

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode("utf-8")
            return r.status, json.loads(body) if body else {}

    def test_index_html(self) -> None:
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("<title>Gold Trader", body)

    def test_api_summary(self) -> None:
        status, body = self._get("/api/summary")
        self.assertEqual(status, 200)
        self.assertIn("config", body)
        self.assertIn("states", body)
        self.assertIn("journal", body)

    def test_api_journal(self) -> None:
        status, body = self._get("/api/journal?limit=10")
        self.assertEqual(status, 200)
        self.assertIn("rows", body)
        self.assertIn("count", body)

    def test_api_stats(self) -> None:
        status, body = self._get("/api/stats")
        self.assertEqual(status, 200)
        self.assertTrue("n" in body or "error" in body)

    def test_api_logs(self) -> None:
        status, body = self._get("/api/logs?file=agent.log&n=5")
        self.assertEqual(status, 200)
        self.assertIn("lines", body)

    def test_api_logs_path_traversal_rejected(self) -> None:
        status, body = self._get("/api/logs?file=../etc/passwd&n=1")
        self.assertEqual(status, 200)
        self.assertEqual(body.get("error"), "invalid log name")

    def test_404_on_unknown(self) -> None:
        try:
            self._get("/api/does-not-exist")
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_api_datasets(self) -> None:
        status, body = self._get("/api/datasets")
        self.assertEqual(status, 200)
        self.assertIn("datasets", body)
        self.assertIsInstance(body["datasets"], list)

    def test_api_strategies_families(self) -> None:
        status, body = self._get("/api/strategies/families")
        self.assertEqual(status, 200)
        self.assertIn("families", body)
        self.assertIn("asian_range_breakout", body["families"])

    def test_api_macro_list(self) -> None:
        status, body = self._get("/api/macro/list")
        self.assertEqual(status, 200)
        self.assertIn("series", body)

    def test_api_bridge_status_offline_safe(self) -> None:
        # Bridge will not be running during tests; endpoint must return JSON, not crash.
        status, body = self._get("/api/bridge/status")
        self.assertEqual(status, 200)
        self.assertIn("online", body)
        self.assertFalse(body["online"])

    def test_api_live_candles_offline_falls_back(self) -> None:
        # Bridge is offline in tests — endpoint tries bridge first, then CSV fallback.
        status, body = self._get("/api/live/candles?timeframe=15&count=50")
        self.assertEqual(status, 200)
        self.assertIn("bars", body)
        self.assertIn("source", body)
        self.assertFalse(body.get("online", True))
        self.assertIn(body["source"], ("bridge", "csv_fallback"))

    def test_api_live_scout_returns_json(self) -> None:
        status, body = self._get("/api/live/scout?timeframe=15")
        self.assertEqual(status, 200)
        self.assertIn("status", body)
        self.assertIn("approval_brief", body)
        self.assertIn("model_alerts", body)
        brief = body["approval_brief"]
        self.assertIn("workflow_steps", brief)
        self.assertIn("formula", brief)

    def test_api_live_candles_prefer_cache_skips_bridge(self) -> None:
        status, body = self._get("/api/live/candles?timeframe=15&count=50&prefer_cache=1")
        self.assertEqual(status, 200)
        self.assertIn("bars", body)
        if body["bars"]:
            self.assertEqual(body["source"], "csv_fallback")
            self.assertFalse(body.get("online", True))

    def test_api_live_zones_returns_array(self) -> None:
        status, body = self._get("/api/live/zones?timeframe=15&count=200")
        self.assertEqual(status, 200)
        self.assertIn("zones", body)
        self.assertIsInstance(body["zones"], list)
        self.assertIn("source", body)
        self.assertIn(body["source"], ("bridge", "csv_fallback"))
        # Each zone must have the canonical shape
        for z in body["zones"][:5]:
            self.assertIn("kind", z)
            self.assertIn("top", z)
            self.assertIn("bot", z)
            self.assertIn("status", z)

    def test_api_live_confluence_returns_points(self) -> None:
        status, body = self._get("/api/live/confluence?timeframes=15,60&count=300&tolerance=0.5")
        self.assertEqual(status, 200)
        self.assertIn("points", body)
        self.assertIsInstance(body["points"], list)
        self.assertIn("timeframes", body)
        self.assertEqual(body["timeframes"], [15, 60])
        for p in body["points"][:3]:
            self.assertIn("price_top", p)
            self.assertIn("price_bot", p)
            self.assertIn("side", p)
            self.assertIn("score", p)
            self.assertIn("contributors", p)

    def test_api_live_tracker_returns_quickly(self) -> None:
        status, body = self._get("/api/live/tracker?timeframe=15&count=200")
        self.assertEqual(status, 200)
        self.assertIn("levels", body)
        self.assertIn("source", body)

    def test_api_ifvg_approve_offline_safe(self) -> None:
        status, body = self._post("/api/ifvg/approve", {
            "side": "long",
            "stop": 2300.0,
            "target": 2350.0,
            "entry": 2310.0,
            "verdict": "valid_entry",
        })
        self.assertEqual(status, 200)
        self.assertFalse(body.get("ok"))
        self.assertIn("error", body)

    def test_api_live_ifvg_checklist_returns_stable_json(self) -> None:
        status, body = self._get("/api/live/ifvg/checklist?timeframe=15&count=200")
        self.assertEqual(status, 200)
        self.assertIn("setups", body)
        self.assertIn("manual_approval_required", body)
        self.assertIsInstance(body["setups"], list)
        for setup in body["setups"][:1]:
            self.assertIn("external_research", setup)
            self.assertIn("ai_verdict", setup)
            self.assertIn("trade_style", setup)

    def test_api_secrets_roundtrip(self) -> None:
        from gold_trader.web.server import SECRETS_PATH
        backup = SECRETS_PATH.read_text() if SECRETS_PATH.exists() else None
        try:
            status, body = self._post("/api/secrets", {"openai_api_key": "sk-ui-test-key"})
            self.assertEqual(status, 200)
            self.assertTrue(body.get("ok"))
            self.assertTrue(body["secrets"]["openai_api_key_set"])
            status2, got = self._get("/api/secrets")
            self.assertEqual(status2, 200)
            self.assertTrue(got["openai_api_key_set"])
            status3, cleared = self._post("/api/secrets", {"clear_openai_api_key": True})
            self.assertEqual(status3, 200)
            self.assertFalse(cleared["secrets"]["openai_api_key_set"])
        finally:
            if backup is not None:
                SECRETS_PATH.write_text(backup)
            elif SECRETS_PATH.exists():
                SECRETS_PATH.unlink()

    def test_api_summary_includes_secrets_status(self) -> None:
        status, body = self._get("/api/summary")
        self.assertEqual(status, 200)
        self.assertIn("secrets", body)

    def test_api_config_accepts_bridge_fields(self) -> None:
        from gold_trader.web.server import CONFIG_PATH
        backup = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else None
        try:
            status, body = self._post("/api/config", {
                "bridge_url": "http://127.0.0.1:9999",
                "bridge_secret": "test-secret",
                "symbol": "EURUSD",
            })
            self.assertEqual(status, 200)
            self.assertTrue(body.get("ok"))
            cfg = body.get("config", {})
            self.assertEqual(cfg.get("bridge_url"), "http://127.0.0.1:9999")
            self.assertEqual(cfg.get("symbol"), "EURUSD")
            self.assertTrue(cfg.get("bridge_secret_set"))
            # bridge_secret should NOT be exposed in plaintext
            self.assertNotIn("bridge_secret", cfg)
        finally:
            if backup is not None:
                CONFIG_PATH.write_text(backup)
            elif CONFIG_PATH.exists():
                CONFIG_PATH.unlink()

    def test_api_config_rejects_bad_bridge_url(self) -> None:
        from gold_trader.web.server import CONFIG_PATH
        backup = CONFIG_PATH.read_text() if CONFIG_PATH.exists() else None
        try:
            status, body = self._post("/api/config", {"bridge_url": "ftp://evil/"})
            self.assertEqual(status, 200)
            # Should have ignored the bad URL, not changed it
            self.assertNotEqual(body["config"].get("bridge_url"), "ftp://evil/")
        finally:
            if backup is not None:
                CONFIG_PATH.write_text(backup)
            elif CONFIG_PATH.exists():
                CONFIG_PATH.unlink()

    def test_api_risk(self) -> None:
        status, body = self._get("/api/risk")
        self.assertEqual(status, 200)
        self.assertIn("equity_curve", body)
        self.assertIn("paper_states", body)

    def test_api_candles_requires_path(self) -> None:
        status, body = self._get("/api/candles")
        self.assertEqual(status, 200)
        self.assertEqual(body.get("error"), "path required")

    def test_api_candles_path_traversal(self) -> None:
        status, body = self._get("/api/candles?path=../../etc/passwd")
        self.assertEqual(status, 200)
        self.assertIn("error", body)

    def test_api_candles_real_file(self) -> None:
        # Use any real CSV in repo
        ds_status, ds_body = self._get("/api/datasets")
        if not ds_body.get("datasets"):
            self.skipTest("no datasets available")
        path = ds_body["datasets"][0]["path"]
        status, body = self._get(f"/api/candles?path={path}&limit=10")
        self.assertEqual(status, 200)
        self.assertIn("bars", body)
        self.assertLessEqual(len(body["bars"]), 10)
        if body["bars"]:
            b = body["bars"][0]
            self.assertIn("time", b)
            self.assertIn("open", b)
            self.assertIn("close", b)

    def test_api_indicators_real_file(self) -> None:
        ds_status, ds_body = self._get("/api/datasets")
        if not ds_body.get("datasets"):
            self.skipTest("no datasets available")
        path = ds_body["datasets"][0]["path"]
        status, body = self._get(f"/api/indicators?path={path}&limit=200")
        self.assertEqual(status, 200)
        self.assertIn("ema20", body)
        self.assertIn("vwap", body)


if __name__ == "__main__":
    unittest.main()
