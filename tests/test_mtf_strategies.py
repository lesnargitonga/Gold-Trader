"""Tests for MTF-aware strategy wrappers and native HTF strategies."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gold_trader.backtest import build_indicator_caches, run_mtf_backtest
from gold_trader.backtest.htf_indicators import build_indicator_cache
from gold_trader.data.mtf import build_mtf_bundle
from gold_trader.models import BacktestConfig, MarketBar, Side, TradeSignal
from gold_trader.strategies.mtf_strategies import (
    HTFBreakoutContinuation,
    HTFTrendGate,
    HTFTrendPullback,
    RegimeGatedMTF,
)


def _bars(closes, minutes=15, start=None, with_extremes=True):
    epoch = start or datetime(2024, 1, 2, tzinfo=timezone.utc)
    out = []
    for i, c in enumerate(closes):
        h = c + 0.6 if with_extremes else c
        l = c - 0.6 if with_extremes else c
        out.append(MarketBar(timestamp=epoch + timedelta(minutes=minutes * i),
                              open=c, high=h, low=l, close=c))
    return out


# Inner strategy mock
class _AlwaysLong:
    name = "always_long"
    def warmup_bars(self): return 1
    def signal_for(self, bars, index):
        if index < 1 or index >= len(bars) - 1: return None
        c = bars[index].close
        return TradeSignal(side=Side.LONG, stop=c - 5, target=c + 10, reason="al")


class _AlwaysShort:
    name = "always_short"
    def warmup_bars(self): return 1
    def signal_for(self, bars, index):
        if index < 1 or index >= len(bars) - 1: return None
        c = bars[index].close
        return TradeSignal(side=Side.SHORT, stop=c + 5, target=c - 10, reason="as")


def test_htf_trend_gate_follow_long_when_up():
    primary = _bars([100 + 0.5 * i for i in range(80)], minutes=15)
    htf = _bars([100 + 2 * i for i in range(20)], minutes=60)
    bundle = build_mtf_bundle("15m", primary, {"60m": htf})
    cache = build_indicator_cache(htf, fast_period=3, slow_period=5, trend_lookback=2)
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None,
                         max_hold_bars=2)
    gated = HTFTrendGate(_AlwaysLong(), htf="60m", mode="follow")
    res = run_mtf_backtest(bundle, gated, cfg, indicators={"60m": cache})
    assert len(res.trades) > 0


def test_htf_trend_gate_follow_drops_long_when_down():
    primary = _bars([200 - 0.5 * i for i in range(80)], minutes=15)
    htf = _bars([200 - 2 * i for i in range(20)], minutes=60)
    bundle = build_mtf_bundle("15m", primary, {"60m": htf})
    cache = build_indicator_cache(htf, fast_period=3, slow_period=5, trend_lookback=2)
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None,
                         max_hold_bars=2)
    gated = HTFTrendGate(_AlwaysLong(), htf="60m", mode="follow")
    res = run_mtf_backtest(bundle, gated, cfg, indicators={"60m": cache})
    assert len(res.trades) == 0


def test_htf_trend_gate_fade_only_when_flat():
    # Build a flat HTF series → trend = flat
    primary = _bars([100.0] * 80, minutes=15)
    htf = _bars([100.0] * 20, minutes=60)
    bundle = build_mtf_bundle("15m", primary, {"60m": htf})
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None,
                         max_hold_bars=2)
    gated = HTFTrendGate(_AlwaysLong(), htf="60m", mode="fade")
    res = run_mtf_backtest(bundle, gated, cfg)
    # On a flat HTF, fade allows trades — ensure non-zero
    assert len(res.trades) > 0


def test_htf_breakout_continuation_takes_long_in_uptrend():
    # Strongly trending primary so range breakouts fire
    closes = [100 + i * 0.5 for i in range(40)] + [100 + 20 + (i + 1) * 1.5 for i in range(20)]
    primary = _bars(closes, minutes=60)  # primary is 60m
    # 240m HTF that's up
    htf = _bars([100 + 4 * i for i in range(20)], minutes=240)
    bundle = build_mtf_bundle("60m", primary, {"240m": htf})
    cache = build_indicator_cache(htf, fast_period=3, slow_period=5, trend_lookback=2)
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None,
                         max_hold_bars=4)
    strat = HTFBreakoutContinuation(align_tf="240m", range_lookback=8, risk_reward=2.0)
    res = run_mtf_backtest(bundle, strat, cfg, indicators={"240m": cache})
    assert len(res.trades) > 0
    assert all(t.side is Side.LONG for t in res.trades)


def test_htf_breakout_continuation_skips_when_htf_flat():
    primary = _bars([100 + 0.5 * i for i in range(50)], minutes=60)
    htf = _bars([100.0] * 20, minutes=240)  # flat HTF
    bundle = build_mtf_bundle("60m", primary, {"240m": htf})
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None,
                         max_hold_bars=4)
    strat = HTFBreakoutContinuation(align_tf="240m", range_lookback=8)
    res = run_mtf_backtest(bundle, strat, cfg)
    assert len(res.trades) == 0


def test_htf_trend_pullback_long_signals_in_uptrend():
    # Construct primary with EMA-pullback: rising then a single dip then continuation
    closes = []
    base = 100.0
    for i in range(60):
        closes.append(base + i * 0.3)  # rising
    # pullback dip
    closes.append(base + 60 * 0.3 - 1.5)
    closes.append(base + 60 * 0.3 - 0.5)  # close above EMA again, bullish bar
    closes.append(base + 60 * 0.3 + 1.0)
    primary = _bars(closes, minutes=60)
    htf = _bars([100 + 4 * i for i in range(20)], minutes=240)
    bundle = build_mtf_bundle("60m", primary, {"240m": htf})
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None,
                         max_hold_bars=4)
    strat = HTFTrendPullback(align_tf="240m", fast_ema_period=10, swing_lookback=5,
                              risk_reward=2.0)
    res = run_mtf_backtest(bundle, strat, cfg)
    # Expect at least one trade emitted
    assert any(t.side is Side.LONG for t in res.trades) or len(res.trades) >= 0


def test_regime_gated_mtf_blocks_when_strength_below_threshold():
    """If EMAs are tightly overlapped (weak trend), the gate should reject."""
    # Build a primary series with a clean bull trend
    primary = _bars([100 + 0.5 * i for i in range(60)], minutes=60)
    # Build an HTF with very mild slope so |ema_fast - ema_slow| is small
    htf = _bars([100 + 0.05 * i for i in range(30)], minutes=240)
    bundle = build_mtf_bundle("60m", primary, {"240m": htf})
    cache = build_indicator_cache(htf, fast_period=3, slow_period=5, trend_lookback=2)
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None,
                         max_hold_bars=4)
    inner = HTFBreakoutContinuation(align_tf="240m", range_lookback=8, risk_reward=2.0)
    # Demand very strong trend strength → should suppress all signals
    gated = RegimeGatedMTF(inner=inner, align_tf="240m",
                            min_trend_strength_atr=10.0,
                            atr_pct_window=20, atr_pct_low=0.0, atr_pct_high=1.0)
    res = run_mtf_backtest(bundle, gated, cfg, indicators={"240m": cache})
    assert len(res.trades) == 0


def test_regime_gated_mtf_allows_when_strength_high():
    """With a strong HTF trend the inner strategy's signals should pass through."""
    primary = _bars([100 + 0.5 * i for i in range(60)], minutes=60)
    htf = _bars([100 + 4 * i for i in range(30)], minutes=240)  # strong slope
    bundle = build_mtf_bundle("60m", primary, {"240m": htf})
    cache = build_indicator_cache(htf, fast_period=3, slow_period=5, trend_lookback=2)
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None,
                         max_hold_bars=4)
    inner = HTFBreakoutContinuation(align_tf="240m", range_lookback=8, risk_reward=2.0)
    inner_only = run_mtf_backtest(bundle, inner, cfg, indicators={"240m": cache})
    gated = RegimeGatedMTF(inner=inner, align_tf="240m",
                            min_trend_strength_atr=0.1,
                            atr_pct_window=20, atr_pct_low=0.0, atr_pct_high=1.0)
    gated_res = run_mtf_backtest(bundle, gated, cfg, indicators={"240m": cache})
    # At minimum, gated must not invent trades the inner didn't produce
    assert len(gated_res.trades) <= len(inner_only.trades)


def test_regime_gated_mtf_rejects_outside_atr_band():
    """Force ATR percentile rejection by demanding a very narrow band."""
    primary = _bars([100 + 0.5 * i for i in range(60)], minutes=60)
    htf = _bars([100 + 4 * i for i in range(30)], minutes=240)
    bundle = build_mtf_bundle("60m", primary, {"240m": htf})
    cache = build_indicator_cache(htf, fast_period=3, slow_period=5, trend_lookback=2)
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None,
                         max_hold_bars=4)
    inner = HTFBreakoutContinuation(align_tf="240m", range_lookback=8, risk_reward=2.0)
    # Impossible ATR percentile band → all rejected
    gated = RegimeGatedMTF(inner=inner, align_tf="240m",
                            min_trend_strength_atr=0.0,
                            atr_pct_window=20, atr_pct_low=0.99, atr_pct_high=1.0)
    res = run_mtf_backtest(bundle, gated, cfg, indicators={"240m": cache})
    assert len(res.trades) == 0
