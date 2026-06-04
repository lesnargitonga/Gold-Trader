"""Tests for HTF indicator cache."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gold_trader.backtest.htf_indicators import (
    HTFIndicatorCache,
    atr_series,
    build_indicator_cache,
    ema_series,
    swing_points,
    true_range_series,
)
from gold_trader.models import MarketBar


def _bar(ts: datetime, o: float, h: float, l: float, c: float) -> MarketBar:
    return MarketBar(timestamp=ts, open=o, high=h, low=l, close=c)


def _series(closes: list[float], minutes: int = 60) -> list[MarketBar]:
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    out = []
    for i, c in enumerate(closes):
        out.append(_bar(epoch + timedelta(minutes=minutes * i), c, c + 1, c - 1, c))
    return out


def test_ema_seed_and_step():
    out = ema_series([10.0, 12.0, 14.0], 3)
    # alpha = 2/4 = 0.5, seed = 10, then 0.5*12 + 0.5*10 = 11, then 0.5*14 + 0.5*11 = 12.5
    assert out == [10.0, 11.0, 12.5]


def test_ema_empty_and_period_validation():
    assert ema_series([], 5) == []
    with pytest.raises(ValueError):
        ema_series([1.0], 0)


def test_true_range_first_bar_uses_high_low():
    bars = _series([100.0, 102.0])
    tr = true_range_series(bars)
    # First bar: high-low = 1 - (-1) = 2 (since synthesized as c+1, c-1)
    assert tr[0] == pytest.approx(2.0)


def test_atr_running_average_seed():
    bars = _series([100.0, 102.0, 104.0])
    atr = atr_series(bars, period=14)
    # During warmup (i < period), it's a running average
    assert len(atr) == 3
    assert all(a > 0 for a in atr)


def test_atr_invalid_period():
    with pytest.raises(ValueError):
        atr_series(_series([1.0, 2.0]), period=0)


def test_swing_points_simple_peak_and_trough():
    # closes: 1 2 3 4 5 4 3  ⇒ index 4 is a swing high
    bars = []
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    highs = [1, 2, 3, 4, 5, 4, 3]
    lows  = [0, 1, 2, 3, 4, 3, 2]
    for i, (h, l) in enumerate(zip(highs, lows)):
        bars.append(MarketBar(
            timestamp=epoch + timedelta(hours=i),
            open=h - 0.5, high=h, low=l, close=h - 0.5,
        ))
    sw = swing_points(bars, left=2, right=2)
    assert any(s.index == 4 and s.kind == "high" for s in sw)


def test_swing_points_with_low():
    bars = []
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    highs = [5, 4, 3, 2, 1, 2, 3]
    lows  = [4, 3, 2, 1, 0, 1, 2]
    for i, (h, l) in enumerate(zip(highs, lows)):
        bars.append(MarketBar(
            timestamp=epoch + timedelta(hours=i),
            open=l + 0.5, high=h, low=l, close=l + 0.5,
        ))
    sw = swing_points(bars, left=2, right=2)
    assert any(s.index == 4 and s.kind == "low" for s in sw)


def test_build_indicator_cache_full():
    # Trending up series: closes 100..160
    bars = _series([100.0 + i for i in range(60)])
    cache = build_indicator_cache(bars, fast_period=10, slow_period=20)
    assert isinstance(cache, HTFIndicatorCache)
    assert len(cache.ema_fast) == len(bars)
    assert len(cache.ema_slow) == len(bars)
    assert len(cache.atr) == len(bars)
    assert len(cache.trend) == len(bars)
    # Last bar should be trending up: close > ema_slow and ema_slow rising
    assert cache.trend[-1] == "up"
    # Early bars (within slow_period warmup) are flat
    assert cache.trend[5] == "flat"


def test_build_indicator_cache_downtrend():
    bars = _series([200.0 - i for i in range(60)])
    cache = build_indicator_cache(bars, fast_period=10, slow_period=20)
    assert cache.trend[-1] == "down"


def test_build_indicator_cache_flat():
    bars = _series([100.0] * 60)
    cache = build_indicator_cache(bars, fast_period=10, slow_period=20)
    # All same price → ema rises/falls don't trigger; check no crash and trend valid
    assert cache.trend[-1] == "flat"


def test_swing_lookup_respects_confirmation_window():
    # Build a series with a clear high at index 5, swing_right=2 means
    # confirmation only at index 7.
    bars = []
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    highs = [1, 2, 3, 4, 5, 6, 5, 4, 3, 2]
    lows  = [0, 1, 2, 3, 4, 5, 4, 3, 2, 1]
    for i, (h, l) in enumerate(zip(highs, lows)):
        bars.append(MarketBar(
            timestamp=epoch + timedelta(hours=i),
            open=h - 0.5, high=h, low=l, close=h - 0.5,
        ))
    cache = build_indicator_cache(
        bars, fast_period=3, slow_period=5,
        swing_left=2, swing_right=2,
    )
    # Look up at idx 5: swing exists at i=5 but not yet confirmed
    assert cache.last_confirmed_swing_high(5) is None
    assert cache.last_confirmed_swing_high(6) is None
    # At idx 7, confirmation point reached
    sw = cache.last_confirmed_swing_high(7)
    assert sw is not None
    assert sw.index == 5
    assert sw.price == 6


def test_indicator_cache_query_bounds_safe():
    bars = _series([100.0 + i for i in range(30)])
    cache = build_indicator_cache(bars)
    assert cache.trend_at(-1) == "flat"
    assert cache.trend_at(9999) == "flat"
    assert cache.ema_fast_at(-1) is None
    assert cache.atr_at(9999) is None
