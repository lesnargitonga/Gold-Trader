"""Tests for LTF entry-trigger refinement."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gold_trader.backtest import (
    Engulf,
    MomentumDisplacement,
    StructureBreak,
    make_ltf_entry_resolver,
    run_backtest,
)
from gold_trader.models import BacktestConfig, MarketBar, Side, TradeSignal


def _bar(ts, o, h, l, c):
    return MarketBar(timestamp=ts, open=o, high=h, low=l, close=c)


def _ltf_window_uptrend(start: datetime, n: int = 5) -> list[MarketBar]:
    return [
        _bar(start + timedelta(minutes=i), 100 + i, 101 + i, 99 + i, 101 + i)
        for i in range(n)
    ]


def _ltf_window_downtrend(start: datetime, n: int = 5) -> list[MarketBar]:
    return [
        _bar(start + timedelta(minutes=i), 100 - i, 101 - i, 99 - i, 99 - i)
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Trigger pattern unit tests
# ---------------------------------------------------------------------------

def test_displacement_long_confirms_on_strong_body():
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    bars = []
    # 14 quiet bars then a displacement
    for i in range(14):
        bars.append(_bar(epoch + timedelta(minutes=i), 100, 100.1, 99.9, 100))
    # bar 14: large bullish body
    bars.append(_bar(epoch + timedelta(minutes=14), 100, 105, 100, 105))
    # bar 15: needed for fill
    bars.append(_bar(epoch + timedelta(minutes=15), 105, 106, 104, 105))

    trig = MomentumDisplacement(body_atr_mult=0.5, atr_period=14)
    j = trig.confirm(Side.LONG, bars)
    assert j == 14


def test_displacement_short_requires_negative_body():
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    bars = []
    for i in range(14):
        bars.append(_bar(epoch + timedelta(minutes=i), 100, 100.1, 99.9, 100))
    bars.append(_bar(epoch + timedelta(minutes=14), 100, 100, 95, 95))
    bars.append(_bar(epoch + timedelta(minutes=15), 95, 96, 94, 95))
    trig = MomentumDisplacement(body_atr_mult=0.5, atr_period=14)
    assert trig.confirm(Side.SHORT, bars) == 14
    # Long-side request shouldn't confirm on a bearish bar
    assert trig.confirm(Side.LONG, bars) is None


def test_engulf_long():
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    bars = [
        _bar(epoch, 100, 100.5, 99.0, 99.5),  # bearish
        _bar(epoch + timedelta(minutes=1), 99.0, 101.0, 98.5, 100.8),  # bullish engulf
        _bar(epoch + timedelta(minutes=2), 100.8, 101.5, 100.0, 101.0),  # for fill
    ]
    trig = Engulf()
    assert trig.confirm(Side.LONG, bars) == 1


def test_engulf_short():
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    bars = [
        _bar(epoch, 99.0, 100.5, 98.5, 100.0),  # bullish
        _bar(epoch + timedelta(minutes=1), 100.5, 101.0, 98.0, 98.5),  # bearish engulf
        _bar(epoch + timedelta(minutes=2), 98.5, 99.0, 97.5, 98.0),
    ]
    trig = Engulf()
    assert trig.confirm(Side.SHORT, bars) == 1


def test_structure_break_long():
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    bars = [
        _bar(epoch, 100, 100.5, 99, 100),
        _bar(epoch + timedelta(minutes=1), 100, 100.6, 99, 100),
        _bar(epoch + timedelta(minutes=2), 100, 100.7, 99, 100),
        _bar(epoch + timedelta(minutes=3), 100, 102, 99.5, 101.5),  # break
        _bar(epoch + timedelta(minutes=4), 101.5, 102.5, 101, 102),
    ]
    trig = StructureBreak(lookback=3)
    assert trig.confirm(Side.LONG, bars) == 3


def test_no_confirmation_returns_none():
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    flat = [_bar(epoch + timedelta(minutes=i), 100, 100.1, 99.9, 100) for i in range(10)]
    assert MomentumDisplacement(body_atr_mult=2.0).confirm(Side.LONG, flat) is None
    assert Engulf().confirm(Side.LONG, flat) is None
    assert StructureBreak(lookback=3).confirm(Side.LONG, flat) is None


# ---------------------------------------------------------------------------
# Resolver / engine integration
# ---------------------------------------------------------------------------

class _AlwaysLong:
    name = "always_long"
    def warmup_bars(self): return 1
    def signal_for(self, bars, index):
        if index < 1 or index >= len(bars) - 1:
            return None
        c = bars[index].close
        return TradeSignal(side=Side.LONG, stop=c - 5.0, target=c + 10.0, reason="test")


def test_resolver_drops_signals_without_confirmation():
    # Primary 15m bars
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    primary = [
        MarketBar(timestamp=epoch + timedelta(minutes=15 * i), open=100.0, high=101, low=99, close=100.0)
        for i in range(20)
    ]
    # Flat 5m series (no displacement)
    ltf = [
        MarketBar(timestamp=epoch + timedelta(minutes=5 * i), open=100, high=100.1, low=99.9, close=100)
        for i in range(60)
    ]
    resolver = make_ltf_entry_resolver(
        ltf, primary_tf="15m",
        trigger=MomentumDisplacement(body_atr_mult=2.0, atr_period=2),
        apply_spread=False,
    )
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None,
                         max_hold_bars=2)
    res_with = run_backtest(primary, _AlwaysLong(), cfg, entry_price_resolver=resolver)
    res_without = run_backtest(primary, _AlwaysLong(), cfg)
    # Without trigger the strategy emits trades; with the displacement trigger
    # on a flat LTF, all signals are dropped.
    assert len(res_without.trades) > 0
    assert len(res_with.trades) == 0


def test_resolver_uses_post_confirmation_open():
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    # One primary signal at index 1 (15m bar), fill window is the bar starting
    # at 00:30 (primary[2].timestamp) to 00:45.
    primary = [
        MarketBar(timestamp=epoch + timedelta(minutes=15 * i), open=100, high=101, low=99, close=100)
        for i in range(4)
    ]
    # 5m LTF: cover 00:30..00:45 with a strong bullish displacement at minute 35
    ltf = []
    # Earlier bars (warmup for ATR)
    for m in range(0, 30, 5):
        ltf.append(MarketBar(timestamp=epoch + timedelta(minutes=m),
                             open=100, high=100.1, low=99.9, close=100))
    # 00:30 bar — flat
    ltf.append(MarketBar(timestamp=epoch + timedelta(minutes=30),
                         open=100, high=100.1, low=99.9, close=100))
    # 00:35 — strong bullish (displacement)
    ltf.append(MarketBar(timestamp=epoch + timedelta(minutes=35),
                         open=100, high=102, low=100, close=102))
    # 00:40 — entry fills here (open of bar AFTER confirmation)
    ltf.append(MarketBar(timestamp=epoch + timedelta(minutes=40),
                         open=102, high=103, low=101.5, close=102.5))

    resolver = make_ltf_entry_resolver(
        ltf, primary_tf="15m",
        trigger=MomentumDisplacement(body_atr_mult=0.5, atr_period=2),
        apply_spread=False,
    )
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None,
                         max_hold_bars=2)
    result = run_backtest(primary, _AlwaysLong(), cfg, entry_price_resolver=resolver)
    # Should have exactly one trade with entry near 102 (the 00:40 LTF open)
    assert len(result.trades) >= 1
    t = result.trades[0]
    assert t.entry_price == pytest.approx(102.0, abs=0.01)


def test_resolver_returns_none_when_window_empty():
    epoch = datetime(2024, 1, 2, tzinfo=timezone.utc)
    primary = [
        MarketBar(timestamp=epoch + timedelta(minutes=15 * i), open=100, high=101, low=99, close=100)
        for i in range(5)
    ]
    # LTF that starts AFTER the last primary bar — no overlap
    ltf = [
        MarketBar(timestamp=epoch + timedelta(hours=10) + timedelta(minutes=5 * i),
                  open=100, high=101, low=99, close=100)
        for i in range(20)
    ]
    resolver = make_ltf_entry_resolver(ltf, primary_tf="15m", trigger=Engulf(),
                                        apply_spread=False)
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None)
    res = run_backtest(primary, _AlwaysLong(), cfg, entry_price_resolver=resolver)
    assert len(res.trades) == 0
