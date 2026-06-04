"""Tests for IFVG execution geometry audit helpers."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from gold_trader.assistants.ifvg_confluence import IFVGAssistantConfig, IFVGCandidate  # noqa: E402
from gold_trader.models import MarketBar, Side  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import ifvg_execution_geometry_audit as audit  # noqa: E402


def _bar(ts: datetime, o: float, h: float, lo: float, c: float) -> MarketBar:
    return MarketBar(timestamp=ts, open=o, high=h, low=lo, close=c, volume=1.0)


def test_zone_sl_short_places_stop_above_gap_top() -> None:
    ts = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(ts, 4600, 4602, 4598, 4599)]
    candidate = IFVGCandidate(
        side=Side.SHORT,
        formation_idx=0,
        impulse_idx=0,
        inversion_idx=0,
        signal_idx=0,
        gap_bot=4596.0,
        gap_top=4598.0,
        sweep_idx=0,
        sweep_level=4605.0,
        source="test",
    )
    cfg = IFVGAssistantConfig()
    plan = audit.zone_sl_plan(bars, candidate, atr=2.0, market_levels=[], cfg=cfg)
    assert plan["sl"] == 4598.0 + max(cfg.stop_buffer_atr * 2.0, 0.1)
    assert plan["sl"] < 4605.0
    assert plan["tp1"] == plan["entry"] - plan["risk_points"]


def test_zone_sl_long_places_stop_below_gap_bot() -> None:
    ts = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    bars = [_bar(ts, 4600, 4602, 4598, 4601)]
    candidate = IFVGCandidate(
        side=Side.LONG,
        formation_idx=0,
        impulse_idx=0,
        inversion_idx=0,
        signal_idx=0,
        gap_bot=4598.0,
        gap_top=4600.0,
        sweep_idx=0,
        sweep_level=4590.0,
        source="test",
    )
    cfg = IFVGAssistantConfig()
    plan = audit.zone_sl_plan(bars, candidate, atr=2.0, market_levels=[], cfg=cfg)
    assert plan["sl"] == 4598.0 - max(cfg.stop_buffer_atr * 2.0, 0.1)
    assert plan["sl"] > 4590.0
    assert plan["tp1"] == plan["entry"] + plan["risk_points"]
