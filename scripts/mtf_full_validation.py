"""Full MTF system validation on 5y XAUUSD.

This is the integration experiment that uses every piece of the new MTF
infrastructure end-to-end:

  - MTFBundle alignment (1m/5m/15m/60m/240m/1440m ladder)
  - HTF indicator caches
  - HTFTrendGate wrapping legacy strategies
  - HTFTrendPullback / HTFBreakoutContinuation native HTF strategies
  - LTF entry-trigger refinement (5m displacement / engulf / structure)
  - 3-fold rotation walk-forward across recent 3y

For each combination it reports per-fold PF / win% / trade count and a
robustness verdict (#folds with PF>=1.0).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.backtest import (
    Engulf, MomentumDisplacement, StructureBreak,
)
from gold_trader.models import BacktestConfig
from gold_trader.research.family_grids import family_spec
from gold_trader.strategies.mtf_strategies import (
    HTFBreakoutContinuation, HTFTrendGate, HTFTrendPullback,
)
from gold_trader.validation import (
    format_report, load_5y_ladder, validate_mtf_strategy,
)


CONFIG = BacktestConfig(
    starting_equity=100_000.0,
    risk_fraction=0.01,
    max_hold_bars=24,
    kill_switch_drawdown_fraction=None,
    slippage_bps=2.0,
    commission_per_trade=1.0,
)

# 3 rolling splits inside recent 3y (2023-05 .. 2026-05)
def _split(label, train_lo, train_hi, test_lo, test_hi):
    return (label,
            datetime(*train_lo, tzinfo=timezone.utc),
            datetime(*train_hi, tzinfo=timezone.utc),
            datetime(*test_lo, tzinfo=timezone.utc),
            datetime(*test_hi, tzinfo=timezone.utc))

SPLITS = [
    _split("A", (2023, 5, 4), (2024, 11, 4), (2024, 11, 4), (2025, 5, 4)),
    _split("B", (2023, 11, 4), (2025, 5, 4), (2025, 5, 4), (2025, 11, 4)),
    _split("C", (2024, 5, 4), (2025, 11, 4), (2025, 11, 4), (2026, 5, 4)),
]


def run_15m_legacy_with_htf_gate():
    """Take recent 3y survivors and gate them on 240m trend."""
    primary, htf = load_5y_ladder(
        primary_tf="15m", htf_tfs=["60m", "240m"],
        time_lo=datetime(2023, 4, 1, tzinfo=timezone.utc),
        time_hi=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    families = ["london_breakout", "momentum_burst", "ny_close_compression",
                "inversion_fair_value_gap"]
    print("\n" + "=" * 80)
    print("PRIMARY 15m + HTF 240m FOLLOW gate (legacy strategies, default params)")
    print("=" * 80)
    for fam in families:
        spec = family_spec(fam)
        # Use default first param-set
        params = list(spec.grid)[0]
        legacy = spec.factory(params)
        gated = HTFTrendGate(legacy, htf="240m", mode="follow")
        rep = validate_mtf_strategy(
            label=fam, strategy=gated,
            primary_tf="15m",
            primary_bars=primary,
            htf_bars_by_tf={"60m": htf["60m"], "240m": htf["240m"]},
            splits=SPLITS,
            config=CONFIG,
        )
        print(format_report(rep))


def run_60m_native_htf():
    """Native 60m strategies aligned on 240m trend."""
    primary, htf = load_5y_ladder(
        primary_tf="60m", htf_tfs=["240m", "1440m"],
        time_lo=datetime(2023, 4, 1, tzinfo=timezone.utc),
        time_hi=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    print("\n" + "=" * 80)
    print("PRIMARY 60m HTF-native strategies (240m trend gate)")
    print("=" * 80)
    for strat in [
        HTFBreakoutContinuation(align_tf="240m", range_lookback=12, risk_reward=2.0),
        HTFBreakoutContinuation(align_tf="240m", range_lookback=24, risk_reward=2.0),
        HTFTrendPullback(align_tf="240m", fast_ema_period=20, swing_lookback=10, risk_reward=2.0),
        HTFTrendPullback(align_tf="240m", fast_ema_period=50, swing_lookback=20, risk_reward=2.0),
    ]:
        rep = validate_mtf_strategy(
            label=f"{strat.name}__rl{getattr(strat, 'range_lookback', '-')}__ema{getattr(strat, 'fast_ema_period', '-')}",
            strategy=strat,
            primary_tf="60m",
            primary_bars=primary,
            htf_bars_by_tf={"240m": htf["240m"], "1440m": htf["1440m"]},
            splits=SPLITS,
            config=CONFIG,
        )
        print(format_report(rep))


def run_240m_native_with_daily():
    """Native 240m strategies aligned on daily (1440m) trend."""
    primary, htf = load_5y_ladder(
        primary_tf="240m", htf_tfs=["1440m"],
        time_lo=datetime(2023, 4, 1, tzinfo=timezone.utc),
        time_hi=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    print("\n" + "=" * 80)
    print("PRIMARY 240m HTF-native strategies (1440m daily trend gate)")
    print("=" * 80)
    for strat in [
        HTFBreakoutContinuation(align_tf="1440m", range_lookback=10, risk_reward=2.0),
        HTFBreakoutContinuation(align_tf="1440m", range_lookback=20, risk_reward=3.0),
        HTFTrendPullback(align_tf="1440m", fast_ema_period=20, swing_lookback=8, risk_reward=2.0),
    ]:
        rep = validate_mtf_strategy(
            label=f"{strat.name}__rl{getattr(strat, 'range_lookback', '-')}__ema{getattr(strat, 'fast_ema_period', '-')}",
            strategy=strat,
            primary_tf="240m",
            primary_bars=primary,
            htf_bars_by_tf={"1440m": htf["1440m"]},
            splits=SPLITS,
            config=CONFIG,
            indicator_overrides={"1440m": {
                "fast_period": 10, "slow_period": 20, "trend_lookback": 3,
            }},
        )
        print(format_report(rep))


def run_15m_with_5m_trigger():
    """15m setups + 5m displacement trigger + 240m HTF gate."""
    from gold_trader.data import load_bars_from_csv
    primary, htf = load_5y_ladder(
        primary_tf="15m", htf_tfs=["240m"],
        time_lo=datetime(2023, 4, 1, tzinfo=timezone.utc),
        time_hi=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    ltf_5m = load_bars_from_csv("data/xauusd_5y/xauusd_5y_5m.csv")
    print("\n" + "=" * 80)
    print("PRIMARY 15m + HTF 240m gate + 5m DISPLACEMENT entry trigger")
    print("=" * 80)
    families = ["london_breakout", "momentum_burst"]
    for fam in families:
        spec = family_spec(fam)
        params = list(spec.grid)[0]
        legacy = spec.factory(params)
        gated = HTFTrendGate(legacy, htf="240m", mode="follow")
        rep = validate_mtf_strategy(
            label=f"{fam}_htf240_disp5m",
            strategy=gated,
            primary_tf="15m",
            primary_bars=primary,
            htf_bars_by_tf={"240m": htf["240m"]},
            splits=SPLITS,
            config=CONFIG,
            ltf_bars=ltf_5m,
            ltf_trigger=MomentumDisplacement(body_atr_mult=0.6, atr_period=14),
            ltf_tf="5m",
        )
        print(format_report(rep))


def main():
    run_15m_legacy_with_htf_gate()
    run_60m_native_htf()
    run_240m_native_with_daily()
    run_15m_with_5m_trigger()


if __name__ == "__main__":
    main()
