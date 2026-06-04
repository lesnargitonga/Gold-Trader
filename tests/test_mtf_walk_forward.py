"""Tests for MTF walk-forward validation harness."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gold_trader.models import BacktestConfig, MarketBar, Side, TradeSignal
from gold_trader.validation import (
    MTFValidationReport,
    format_report,
    slice_window,
    validate_mtf_strategy,
)


def _bars(closes, minutes=15, start=None):
    epoch = start or datetime(2024, 1, 2, tzinfo=timezone.utc)
    return [
        MarketBar(timestamp=epoch + timedelta(minutes=minutes * i),
                  open=c, high=c + 0.5, low=c - 0.5, close=c)
        for i, c in enumerate(closes)
    ]


class _AlwaysLong:
    name = "always_long"
    def warmup_bars(self): return 1
    def signal_for(self, bars, index):
        if index < 1 or index >= len(bars) - 1: return None
        c = bars[index].close
        return TradeSignal(side=Side.LONG, stop=c - 5, target=c + 10, reason="t")


def test_slice_window_filters_primary_and_pads_htf():
    epoch = datetime(2024, 1, 1, tzinfo=timezone.utc)
    primary = _bars(list(range(100, 200)), minutes=60, start=epoch)
    htf = _bars(list(range(100, 200)), minutes=240, start=epoch)
    lo = epoch + timedelta(days=1)
    hi = epoch + timedelta(days=2)
    p, h = slice_window(primary, {"240m": htf}, lo, hi, htf_pad_days=10)
    assert all(lo <= b.timestamp <= hi for b in p)
    assert all(b.timestamp <= hi for b in h["240m"])
    # HTF goes back further than lo (padded)
    assert any(b.timestamp < lo for b in h["240m"])


def test_validate_mtf_strategy_runs_three_folds():
    epoch = datetime(2024, 1, 1, tzinfo=timezone.utc)
    primary = _bars([100 + 0.1 * i for i in range(2000)], minutes=60, start=epoch)
    htf = _bars([100 + 0.4 * i for i in range(500)], minutes=240, start=epoch)

    splits = [
        ("A", epoch, epoch + timedelta(days=20),
         epoch + timedelta(days=20), epoch + timedelta(days=30)),
        ("B", epoch + timedelta(days=10), epoch + timedelta(days=30),
         epoch + timedelta(days=30), epoch + timedelta(days=40)),
        ("C", epoch + timedelta(days=20), epoch + timedelta(days=40),
         epoch + timedelta(days=40), epoch + timedelta(days=50)),
    ]
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None,
                         max_hold_bars=4)
    rep = validate_mtf_strategy(
        label="test_always_long",
        strategy=_AlwaysLong(),
        primary_tf="60m",
        primary_bars=primary,
        htf_bars_by_tf={"240m": htf},
        splits=splits,
        config=cfg,
        indicator_overrides={"240m": {"fast_period": 3, "slow_period": 5, "trend_lookback": 2}},
    )
    assert isinstance(rep, MTFValidationReport)
    assert len(rep.folds) == 3
    assert rep.primary_tf == "60m"
    assert rep.htf_codes == ("240m",)
    text = format_report(rep)
    assert "test_always_long" in text
    assert "fold" in text


def test_validate_mtf_strategy_skips_empty_test_window():
    epoch = datetime(2024, 1, 1, tzinfo=timezone.utc)
    primary = _bars([100.0] * 100, minutes=60, start=epoch)
    htf = _bars([100.0] * 30, minutes=240, start=epoch)
    splits = [
        # Test window in the future — no primary bars there
        ("future", epoch, epoch + timedelta(days=1),
         epoch + timedelta(days=999), epoch + timedelta(days=1000)),
    ]
    cfg = BacktestConfig(starting_equity=10_000.0, kill_switch_drawdown_fraction=None)
    rep = validate_mtf_strategy(
        label="empty",
        strategy=_AlwaysLong(),
        primary_tf="60m",
        primary_bars=primary,
        htf_bars_by_tf={"240m": htf},
        splits=splits,
        config=cfg,
    )
    assert len(rep.folds) == 0
