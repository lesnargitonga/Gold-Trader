"""Tests for MTFContext per-bar query interface."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gold_trader.backtest.htf_indicators import build_indicator_cache
from gold_trader.backtest.mtf_context import MTFContext, MTFStrategy
from gold_trader.data.mtf import build_mtf_bundle
from gold_trader.models import MarketBar


def _series(closes: list[float], minutes: int = 15, start: datetime | None = None) -> list[MarketBar]:
    epoch = start or datetime(2024, 1, 2, tzinfo=timezone.utc)
    return [
        MarketBar(
            timestamp=epoch + timedelta(minutes=minutes * i),
            open=c, high=c + 0.5, low=c - 0.5, close=c,
        )
        for i, c in enumerate(closes)
    ]


def _build_ctx(primary_closes, htf60_closes):
    primary = _series(primary_closes, minutes=15)
    htf60 = _series(htf60_closes, minutes=60)
    bundle = build_mtf_bundle("15m", primary, {"60m": htf60})
    cache = build_indicator_cache(htf60, fast_period=3, slow_period=5, trend_lookback=2)
    return MTFContext(bundle=bundle, indicators={"60m": cache})


def test_warmup_returns_minus_one_and_flat():
    ctx = _build_ctx([100.0] * 20, [100.0] * 6)
    ctx0 = ctx.at(0)
    assert ctx0.htf_index("60m") == -1
    assert ctx0.htf_bar("60m") is None
    assert ctx0.trend("60m") == "flat"
    assert ctx0.ema_slow("60m") is None
    assert ctx0.atr("60m") is None


def test_post_warmup_resolves_indicators():
    # 20 primary 15m bars (5 hours) + 6 HTF 60m bars
    primary = _series([100.0 + i for i in range(20)], minutes=15)
    htf60 = _series([100.0 + 4 * i for i in range(6)], minutes=60)
    bundle = build_mtf_bundle("15m", primary, {"60m": htf60})
    cache = build_indicator_cache(htf60, fast_period=2, slow_period=3, trend_lookback=1)
    ctx = MTFContext(bundle=bundle, indicators={"60m": cache})

    # primary index 19 = 04:45  ⇒ latest closed 60m bar is index 3 (04:00 closes 05:00 — too late)
    # primary[19].timestamp = 04:45.  htf60[3].timestamp = 03:00, closes 04:00 → available.
    # htf60[4].timestamp = 04:00, closes 05:00 → NOT yet available at 04:45.
    final = ctx.at(19)
    idx = final.htf_index("60m")
    assert idx == 3
    assert final.htf_bar("60m") is htf60[3]
    assert final.ema_slow("60m") is not None
    assert final.atr("60m") is not None


def test_unknown_htf_methods_return_safe_defaults():
    ctx = _build_ctx([100.0] * 5, [100.0] * 3)
    c = ctx.at(0)
    assert c.has_htf("60m") is True
    assert c.has_htf("240m") is False
    # Querying an HTF not in indicators returns None / flat
    assert c.trend("240m") == "flat"
    assert c.ema_slow("240m") is None
    assert c.atr("240m") is None
    assert c.last_swing_high("240m") is None


def test_swing_lookup_propagates():
    primary = _series([100.0] * 60, minutes=15)
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    # Build an HTF series with a clear high at htf index 5
    highs = [1, 2, 3, 4, 5, 6, 5, 4, 3, 2, 1, 1, 1, 1, 1]
    htf60 = []
    for i, h in enumerate(highs):
        htf60.append(MarketBar(
            timestamp=epoch + timedelta(hours=i),
            open=h - 0.5, high=h, low=h - 1, close=h - 0.5,
        ))
    bundle = build_mtf_bundle("15m", primary, {"60m": htf60})
    cache = build_indicator_cache(htf60, fast_period=2, slow_period=3, swing_left=2, swing_right=2)
    ctx = MTFContext(bundle=bundle, indicators={"60m": cache})

    # Find a primary index whose HTF idx >= 7 (when swing@5 is confirmed)
    # Each htf bar = 4 primary 15m bars.  htf idx 7 confirmed at primary
    # corresponding to htf time = epoch + 8h (htf idx 8 close).
    final = ctx.at(40)  # primary 40 * 15m = 10h after epoch
    sw = final.last_swing_high("60m")
    assert sw is not None
    assert sw.kind == "high"


class _DummyMTFStrat:
    name = "dummy"
    def warmup_bars(self): return 0
    def required_htf(self): return ("60m",)
    def signal_for_mtf(self, bars, index, mtf): return None


def test_mtf_strategy_protocol_runtime_check():
    s = _DummyMTFStrat()
    assert isinstance(s, MTFStrategy)
