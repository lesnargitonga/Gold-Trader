"""Phase 9: regime-gated MTF validation across full 5y, 5 folds.

Tests the *same* HTFBreakoutContinuation configurations that survived
3-fold rotation but bled in Y1/Y2 chop, this time wrapped in
RegimeGatedMTF (HTF trend-strength + ATR-percentile filters).

Goal: cut Y1/Y2 trade volume substantially without destroying Y3-Y5
profitability. If the gate selectively suppresses chop years, it's a
real regime filter; if it suppresses all years equally, it's noise.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.models import BacktestConfig
from gold_trader.strategies.mtf_strategies import (
    HTFBreakoutContinuation, RegimeGatedMTF,
)
from gold_trader.validation import format_report, load_5y_ladder, validate_mtf_strategy


CONFIG = BacktestConfig(
    starting_equity=100_000.0,
    risk_fraction=0.01,
    max_hold_bars=24,
    kill_switch_drawdown_fraction=None,
    slippage_bps=2.0,
    commission_per_trade=1.0,
)


def _split(label, lo, hi):
    return (label,
            datetime(*lo, tzinfo=timezone.utc),
            datetime(*hi, tzinfo=timezone.utc),
            datetime(*lo, tzinfo=timezone.utc),
            datetime(*hi, tzinfo=timezone.utc))


SPLITS = [
    _split("Y1", (2021, 5, 4), (2022, 5, 4)),
    _split("Y2", (2022, 5, 4), (2023, 5, 4)),
    _split("Y3", (2023, 5, 4), (2024, 5, 4)),
    _split("Y4", (2024, 5, 4), (2025, 5, 4)),
    _split("Y5", (2025, 5, 4), (2026, 5, 4)),
]


def _run(label, strategy, primary_tf, primary_bars, htf_bars_by_tf, **kwargs):
    rep = validate_mtf_strategy(
        label=label,
        strategy=strategy,
        primary_tf=primary_tf,
        primary_bars=primary_bars,
        htf_bars_by_tf=htf_bars_by_tf,
        splits=SPLITS,
        config=CONFIG,
        **kwargs,
    )
    print(format_report(rep))


def main():
    # ===== 60m primary, 240m HTF =====================================
    primary60, htf60 = load_5y_ladder(
        primary_tf="60m", htf_tfs=["240m", "1440m"],
        time_lo=datetime(2021, 4, 1, tzinfo=timezone.utc),
        time_hi=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    print("\n" + "=" * 80)
    print("PRIMARY 60m + 240m HTF — REGIME-GATED variants (5-fold)")
    print("=" * 80)
    base_60 = HTFBreakoutContinuation(align_tf="240m", range_lookback=24, risk_reward=3.0)
    print(f"\n--- BASELINE (no regime gate) ---")
    _run("baseline_60m_rl24_rr3", base_60, "60m", primary60,
         {"240m": htf60["240m"], "1440m": htf60["1440m"]})

    for ts_min, lo, hi, lbl in [
        (0.3, 0.10, 0.95, "ts0.3_atr10-95"),
        (0.5, 0.10, 0.95, "ts0.5_atr10-95"),
        (0.5, 0.20, 0.90, "ts0.5_atr20-90"),
        (0.7, 0.10, 0.95, "ts0.7_atr10-95"),
        (1.0, 0.10, 0.95, "ts1.0_atr10-95"),
    ]:
        gated = RegimeGatedMTF(
            inner=base_60, align_tf="240m",
            min_trend_strength_atr=ts_min,
            atr_pct_window=100, atr_pct_low=lo, atr_pct_high=hi,
        )
        print(f"\n--- GATED: {lbl} ---")
        _run(f"gated_60m_rl24_rr3_{lbl}", gated, "60m", primary60,
             {"240m": htf60["240m"], "1440m": htf60["1440m"]})

    # ===== 240m primary, 1440m HTF ===================================
    primary240, htf240 = load_5y_ladder(
        primary_tf="240m", htf_tfs=["1440m"],
        time_lo=datetime(2021, 4, 1, tzinfo=timezone.utc),
        time_hi=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    print("\n" + "=" * 80)
    print("PRIMARY 240m + 1440m daily HTF — REGIME-GATED variants (5-fold)")
    print("=" * 80)
    base_240 = HTFBreakoutContinuation(align_tf="1440m", range_lookback=20, risk_reward=3.0)
    print(f"\n--- BASELINE (no regime gate) ---")
    _run("baseline_240m_rl20_rr3", base_240, "240m", primary240,
         {"1440m": htf240["1440m"]},
         indicator_overrides={"1440m": {"fast_period": 10, "slow_period": 20, "trend_lookback": 3}})

    for ts_min, lo, hi, lbl in [
        (0.3, 0.10, 0.95, "ts0.3_atr10-95"),
        (0.5, 0.10, 0.95, "ts0.5_atr10-95"),
        (0.5, 0.20, 0.90, "ts0.5_atr20-90"),
        (0.7, 0.10, 0.95, "ts0.7_atr10-95"),
        (1.0, 0.10, 0.95, "ts1.0_atr10-95"),
    ]:
        gated = RegimeGatedMTF(
            inner=base_240, align_tf="1440m",
            min_trend_strength_atr=ts_min,
            atr_pct_window=60, atr_pct_low=lo, atr_pct_high=hi,
        )
        print(f"\n--- GATED: {lbl} ---")
        _run(f"gated_240m_rl20_rr3_{lbl}", gated, "240m", primary240,
             {"1440m": htf240["1440m"]},
             indicator_overrides={"1440m": {"fast_period": 10, "slow_period": 20, "trend_lookback": 3}})


if __name__ == "__main__":
    main()
