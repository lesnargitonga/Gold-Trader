from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gold_trader.research.realtime_research import (
    OpenAIResearchConfig,
    RealtimeResearchResult,
    run_openai_market_research,
    should_external_block,
)


def _payload(**overrides):
    data = {
        "symbol": "XAUUSD",
        "side": "sell",
        "current_price": 100.0,
        "ifvg_zone_low": 101.0,
        "ifvg_zone_high": 110.0,
        "entry_low": 101.0,
        "entry_high": 110.0,
        "stop_loss": 111.0,
        "tp1": 99.0,
        "tp2": 98.0,
        "tp3": 97.0,
        "technical_score": 80,
        "checklist_rows": [],
        "market_levels": [],
    }
    data.update(overrides)
    return data


def _write_cfg(path: Path, *, enabled: bool = True, mode: str = "soft") -> None:
    path.write_text(json.dumps({
        "enabled": enabled,
        "mode": mode,
        "model": "gpt-5.4",
        "cache_minutes": 10,
        "max_calls_per_hour": 12,
        "min_ifvg_score_to_research": 65,
        "block_on_high_news_risk": True,
        "block_if_external_context_opposes_trade": True,
    }))


class RealtimeResearchTests(unittest.TestCase):
    def test_disabled_returns_safe_neutral_result(self) -> None:
        with TemporaryDirectory() as td, patch.dict(os.environ, {"GOLD_OPENAI_RESEARCH": "off"}, clear=False):
            cfg = Path(td) / "cfg.json"
            cache = Path(td) / "cache.json"
            _write_cfg(cfg, enabled=True, mode="soft")
            result = run_openai_market_research(**_payload(), config_path=cfg, cache_path=cache)
            self.assertEqual(result.bias, "unknown")
            self.assertFalse(result.supports_trade)
            self.assertFalse(result.should_block_trade)

    def test_missing_api_key_does_not_crash(self) -> None:
        with TemporaryDirectory() as td, patch.dict(os.environ, {"GOLD_OPENAI_RESEARCH": "soft"}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            cfg = Path(td) / "cfg.json"
            cache = Path(td) / "cache.json"
            _write_cfg(cfg)
            with patch("gold_trader.infra.secrets.resolve_openai_api_key", return_value=""):
                result = run_openai_market_research(**_payload(), config_path=cfg, cache_path=cache)
            self.assertIn("External OpenAI research unavailable", " ".join(result.warnings))

    def test_api_key_from_secrets_file(self) -> None:
        with TemporaryDirectory() as td, patch.dict(os.environ, {"GOLD_OPENAI_RESEARCH": "soft"}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            cfg = Path(td) / "cfg.json"
            cache = Path(td) / "cache.json"
            secrets = Path(td) / "secrets.json"
            _write_cfg(cfg)
            with patch("gold_trader.infra.secrets.resolve_openai_api_key", return_value="sk-file-key"):
                with patch("gold_trader.research.realtime_research._call_openai_with_web_search") as mock_call:
                    mock_call.return_value = {
                        "timestamp_utc": "now",
                        "symbol": "XAUUSD",
                        "bias": "bullish_gold",
                        "supports_trade": True,
                        "should_block_trade": False,
                        "confidence": 70,
                        "news_risk": "low",
                        "macro": {"dxy_bias": "supports_buy", "us10y_bias": "neutral", "real_yield_bias": "unknown"},
                        "options": {"bias": "neutral", "important_levels": [], "danger_zones": [], "notes": ""},
                        "warnings": [],
                        "summary": "ok",
                        "sources": ["example.com"],
                    }
                    result = run_openai_market_research(**_payload(), config_path=cfg, cache_path=cache)
            self.assertEqual(result.bias, "bullish_gold")
            mock_call.assert_called_once()

    def test_cached_research_is_reused(self) -> None:
        with TemporaryDirectory() as td, patch.dict(os.environ, {"GOLD_OPENAI_RESEARCH": "soft"}, clear=False):
            cfg = Path(td) / "cfg.json"
            cache = Path(td) / "cache.json"
            _write_cfg(cfg)
            first = run_openai_market_research(**_payload(), config_path=cfg, cache_path=cache)
            with patch("urllib.request.urlopen", side_effect=AssertionError("should not call")):
                second = run_openai_market_research(**_payload(), config_path=cfg, cache_path=cache)
            self.assertEqual(first.summary, second.summary)
            self.assertTrue(second.raw.get("cache_hit"))

    def test_soft_mode_never_blocks(self) -> None:
        cfg = OpenAIResearchConfig(enabled=True, mode="soft", block_on_high_news_risk=True)
        result = RealtimeResearchResult(
            timestamp_utc="now",
            symbol="XAUUSD",
            bias="bearish_gold",
            supports_trade=False,
            should_block_trade=True,
            confidence=90,
            news_risk="high",
            macro={"dxy_bias": "supports_sell", "us10y_bias": "supports_sell", "real_yield_bias": "unknown"},
            options={"bias": "unknown", "important_levels": [], "danger_zones": [], "notes": ""},
            warnings=[],
            summary="",
            sources=[],
        )
        self.assertFalse(should_external_block(result, "buy", cfg))

    def test_hard_mode_can_block(self) -> None:
        cfg = OpenAIResearchConfig(enabled=True, mode="hard", block_on_high_news_risk=True)
        result = RealtimeResearchResult(
            timestamp_utc="now",
            symbol="XAUUSD",
            bias="mixed",
            supports_trade=False,
            should_block_trade=False,
            confidence=60,
            news_risk="high",
            macro={"dxy_bias": "unknown", "us10y_bias": "unknown", "real_yield_bias": "unknown"},
            options={"bias": "unknown", "important_levels": [], "danger_zones": [], "notes": ""},
            warnings=[],
            summary="",
            sources=[],
        )
        self.assertTrue(should_external_block(result, "sell", cfg))


if __name__ == "__main__":
    unittest.main()
