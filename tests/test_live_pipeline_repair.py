"""Tests for live pipeline repair data contracts."""
from __future__ import annotations

import json
from pathlib import Path

from gold_trader.core import live_pipeline_repair as lpr


def _patch_roots(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lpr, "ROOT", tmp_path)
    monkeypatch.setattr(lpr, "LOGS", tmp_path / "logs")
    monkeypatch.setattr(lpr, "DATA", tmp_path / "data")
    (tmp_path / "logs").mkdir()
    (tmp_path / "data").mkdir()


def test_cached_feed_states_and_manual_options_are_preserved(tmp_path: Path, monkeypatch) -> None:
    _patch_roots(monkeypatch, tmp_path)
    for key in ("CME_API_KEY", "CME_CLIENT_ID", "OPTIONS_API_KEY", "OPTIONS_FEED_URL", "TWELVE_DATA_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "data" / "cot").mkdir()
    (tmp_path / "data" / "cot" / "gold_cot_state.json").write_text(
        json.dumps({"state": "available", "source": "fmp_cot", "summary": "latest loaded", "updated_at": lpr.iso_now()})
    )
    (tmp_path / "logs" / "cross_market_state.json").write_text(
        json.dumps({"state": "available", "source": "twelvedata_quote", "notes": ["DXY -0.2%"], "updated_at": lpr.iso_now()})
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "market_levels.json").write_text(
        json.dumps({"levels": [{"price": 4500, "kind": "round", "label": "4500 OI"}]})
    )

    cot = lpr.cot_state()
    cross = lpr.cross_market_state()
    options = lpr.market_levels_state()
    cme = lpr.cme_state()

    assert cot["state"] == "available"
    assert cot["source"] == "fmp_cot"
    assert cross["state"] == "available"
    assert cross["notes"] == ["DXY -0.2%"]
    assert options["state"] == "manual_proxy"
    assert options["levels_count"] == 1
    assert cme["state"] == "missing_credentials"


def test_repair_timeframe_candles_repairs_missing_rows(monkeypatch) -> None:
    monkeypatch.setenv("GOLD_REPAIR_FETCH_WORKERS", "3")

    def fake_fetch(tf: str, count: int | None = None):
        return ([{"time": "2026-06-04T10:00:00+00:00", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 0}], None)

    monkeypatch.setattr(lpr, "fetch_twelve_candles", fake_fetch)
    decision = {"timeframe_reads": [{"timeframe": "M15", "candles": 0, "warnings": ["no live/cached candle data"]}]}

    errors = lpr.repair_timeframe_candles(decision)

    reads = {row["timeframe"]: row for row in decision["timeframe_reads"]}
    assert errors == {}
    assert set(reads) == set(lpr.TIMEFRAMES)
    assert reads["M15"]["candles"] == 1
    assert reads["M15"]["current_price"] == 2
    assert "no live/cached candle data" not in reads["M15"]["warnings"]


def test_build_provider_health_includes_auxiliary_feeds(tmp_path: Path, monkeypatch) -> None:
    _patch_roots(monkeypatch, tmp_path)
    context = {
        "macro_state": {"state": "unknown", "source": "fmp", "error": "403"},
        "sentiment_state": {"state": "neutral", "source": "finnhub", "fresh": True},
        "spread_state": {"state": "unknown_nonfatal_in_paper", "source": "twelvedata", "error": "429"},
        "volatility_state": {"state": "normal", "source": "twelvedata_M15", "atr": 4.0},
        "cot_state": {"state": "available", "source": "fmp_cot", "summary": "loaded"},
        "cross_market_state": {"state": "available", "source": "twelvedata_quote", "notes": ["DXY -0.2%"]},
        "cme_state": {"state": "missing_credentials", "source": "cme_direct_or_vendor", "configured": False},
        "options_state": {"state": "manual_proxy", "source": "options_vendor_or_market_levels_json", "levels_count": 1},
    }
    decision = {
        "timestamp_utc": lpr.iso_now(),
        "timeframe_reads": [{"timeframe": "M15", "candles": 20}],
    }

    health = lpr.build_provider_health(decision, context, {})

    assert health["cot"]["state"] == "available"
    assert health["cross_market"]["notes"] == ["DXY -0.2%"]
    assert health["options"]["state"] == "manual_proxy"
    assert health["fmp_macro"]["error"] == "403"
