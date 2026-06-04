"""Tests for run_mtf_backtest."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gold_trader.backtest import (
    HTFIndicatorCache,
    MTFContext,
    build_indicator_caches,
    run_backtest,
    run_mtf_backtest,
)
from gold_trader.backtest.htf_indicators import build_indicator_cache
from gold_trader.data.mtf import build_mtf_bundle
from gold_trader.models import BacktestConfig, MarketBar, Side, TradeSignal


def _make_bars(closes: list[float], minutes: int, start: datetime | None = None) -> list[MarketBar]:
    epoch = start or datetime(2024, 1, 2, tzinfo=timezone.utc)
    return [
        MarketBar(
            timestamp=epoch + timedelta(minutes=minutes * i),
            open=c,
            high=c + 0.6,
            low=c - 0.6,
            close=c,
        )
        for i, c in enumerate(closes)
    ]


# --------------------------------------------------------------------------
# Fixtures: simple "always-long" strategies
# --------------------------------------------------------------------------

class _LegacyAlwaysLong:
    name = "legacy"

    def warmup_bars(self):
        return 1

    def signal_for(self, bars, index):
        if index < 1 or index >= len(bars) - 1:
            return None
        c = bars[index].close
        return TradeSignal(
            side=Side.LONG,
            stop=c - 1.0,
            target=c + 2.0,
            reason="legacy_always",
        )


class _MTFTrendOnlyLong:
    name = "mtf_trend_long"

    def warmup_bars(self):
        return 1

    def required_htf(self):
        return ("60m",)

    def signal_for_mtf(self, bars, index, mtf):
        if index < 1 or index >= len(bars) - 1:
            return None
        if mtf.trend("60m") != "up":
            return None
        c = bars[index].close
        return TradeSignal(
            side=Side.LONG,
            stop=c - 1.0,
            target=c + 2.0,
            reason="mtf_trend_long",
        )


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_legacy_strategy_runs_through_mtf_engine():
    primary = _make_bars([100.0 + i for i in range(40)], minutes=15)
    htf60 = _make_bars([100.0 + 4 * i for i in range(10)], minutes=60)
    bundle = build_mtf_bundle("15m", primary, {"60m": htf60})
    cfg = BacktestConfig(starting_equity=10_000.0, risk_fraction=0.01,
                         kill_switch_drawdown_fraction=None)
    result = run_mtf_backtest(bundle, _LegacyAlwaysLong(), cfg)
    # Should produce trades same as legacy run_backtest on primary alone
    legacy_only = run_backtest(primary, _LegacyAlwaysLong(), cfg)
    assert result.strategy_name == legacy_only.strategy_name
    assert len(result.trades) == len(legacy_only.trades)


def test_mtf_strategy_skips_during_htf_warmup():
    primary = _make_bars([100.0 + i for i in range(8)], minutes=15)  # only 2h
    htf60 = _make_bars([100.0, 102.0], minutes=60)  # only 2 bars, neither closed enough
    bundle = build_mtf_bundle("15m", primary, {"60m": htf60})
    cfg = BacktestConfig(starting_equity=10_000.0, risk_fraction=0.01,
                         kill_switch_drawdown_fraction=None, max_hold_bars=4)
    result = run_mtf_backtest(bundle, _MTFTrendOnlyLong(), cfg)
    # No HTF closed in time + trend cache short → no trades
    assert len(result.trades) == 0


def test_mtf_strategy_emits_when_trend_aligns():
    # 60 primary 15m bars (15h) with rising prices ⇒ 60m trend = up
    primary = _make_bars([100.0 + 0.5 * i for i in range(80)], minutes=15)
    htf60 = _make_bars([100.0 + 2.0 * i for i in range(20)], minutes=60)
    bundle = build_mtf_bundle("15m", primary, {"60m": htf60})

    cache = build_indicator_cache(htf60, fast_period=3, slow_period=5, trend_lookback=2)
    indicators = {"60m": cache}

    cfg = BacktestConfig(starting_equity=10_000.0, risk_fraction=0.01,
                         kill_switch_drawdown_fraction=None, max_hold_bars=4)
    result = run_mtf_backtest(bundle, _MTFTrendOnlyLong(), cfg, indicators=indicators)
    assert len(result.trades) > 0
    # All trades long-only
    assert all(t.side is Side.LONG for t in result.trades)


def test_mtf_strategy_skips_when_trend_opposite():
    # Falling price ⇒ trend = down; long-only strategy should produce 0 trades
    primary = _make_bars([200.0 - 0.5 * i for i in range(80)], minutes=15)
    htf60 = _make_bars([200.0 - 2.0 * i for i in range(20)], minutes=60)
    bundle = build_mtf_bundle("15m", primary, {"60m": htf60})
    cache = build_indicator_cache(htf60, fast_period=3, slow_period=5, trend_lookback=2)
    cfg = BacktestConfig(starting_equity=10_000.0, risk_fraction=0.01,
                         kill_switch_drawdown_fraction=None, max_hold_bars=4)
    result = run_mtf_backtest(bundle, _MTFTrendOnlyLong(), cfg, indicators={"60m": cache})
    assert len(result.trades) == 0


def test_build_indicator_caches_covers_every_htf():
    primary = _make_bars([100.0] * 40, minutes=15)
    htf60 = _make_bars([100.0] * 10, minutes=60)
    htf240 = _make_bars([100.0] * 4, minutes=240)
    bundle = build_mtf_bundle("15m", primary, {"60m": htf60, "240m": htf240})
    caches = build_indicator_caches(bundle)
    assert set(caches.keys()) == {"60m", "240m"}
    assert isinstance(caches["60m"], HTFIndicatorCache)
    assert isinstance(caches["240m"], HTFIndicatorCache)
