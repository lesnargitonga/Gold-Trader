"""5-fold rotation across full 5y to harden the survivors.

Confirms the 240m HTFBreakoutContinuation finding under more independent
forward windows than the 3-fold recent-3y test.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gold_trader.models import BacktestConfig
from gold_trader.strategies.mtf_strategies import HTFBreakoutContinuation
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
            datetime(*lo, tzinfo=timezone.utc),  # train_lo (unused)
            datetime(*hi, tzinfo=timezone.utc),  # train_hi (unused)
            datetime(*lo, tzinfo=timezone.utc),  # test_lo
            datetime(*hi, tzinfo=timezone.utc))  # test_hi


SPLITS_60M = [
    _split("Y1", (2021, 5, 4), (2022, 5, 4)),
    _split("Y2", (2022, 5, 4), (2023, 5, 4)),
    _split("Y3", (2023, 5, 4), (2024, 5, 4)),
    _split("Y4", (2024, 5, 4), (2025, 5, 4)),
    _split("Y5", (2025, 5, 4), (2026, 5, 4)),
]

SPLITS_240M = SPLITS_60M  # same windows


def main():
    # ---- 60m primary, 240m HTF -----------------------------------------
    primary60, htf60 = load_5y_ladder(
        primary_tf="60m", htf_tfs=["240m", "1440m"],
        time_lo=datetime(2021, 4, 1, tzinfo=timezone.utc),
        time_hi=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    print("\n" + "=" * 80)
    print("PRIMARY 60m + 240m HTF gate (5-fold full-5y rotation)")
    print("=" * 80)
    for cfg in [
        HTFBreakoutContinuation(align_tf="240m", range_lookback=12, risk_reward=2.0),
        HTFBreakoutContinuation(align_tf="240m", range_lookback=24, risk_reward=2.0),
        HTFBreakoutContinuation(align_tf="240m", range_lookback=24, risk_reward=3.0),
    ]:
        rep = validate_mtf_strategy(
            label=f"{cfg.name}_rl{cfg.range_lookback}_rr{cfg.risk_reward}",
            strategy=cfg,
            primary_tf="60m",
            primary_bars=primary60,
            htf_bars_by_tf={"240m": htf60["240m"], "1440m": htf60["1440m"]},
            splits=SPLITS_60M,
            config=CONFIG,
        )
        print(format_report(rep))

    # ---- 240m primary, 1440m HTF ---------------------------------------
    primary240, htf240 = load_5y_ladder(
        primary_tf="240m", htf_tfs=["1440m"],
        time_lo=datetime(2021, 4, 1, tzinfo=timezone.utc),
        time_hi=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )
    print("\n" + "=" * 80)
    print("PRIMARY 240m + 1440m daily HTF gate (5-fold full-5y rotation)")
    print("=" * 80)
    for cfg in [
        HTFBreakoutContinuation(align_tf="1440m", range_lookback=10, risk_reward=2.0),
        HTFBreakoutContinuation(align_tf="1440m", range_lookback=20, risk_reward=2.0),
        HTFBreakoutContinuation(align_tf="1440m", range_lookback=20, risk_reward=3.0),
    ]:
        rep = validate_mtf_strategy(
            label=f"{cfg.name}_rl{cfg.range_lookback}_rr{cfg.risk_reward}",
            strategy=cfg,
            primary_tf="240m",
            primary_bars=primary240,
            htf_bars_by_tf={"1440m": htf240["1440m"]},
            splits=SPLITS_240M,
            config=CONFIG,
            indicator_overrides={"1440m": {
                "fast_period": 10, "slow_period": 20, "trend_lookback": 3,
            }},
        )
        print(format_report(rep))


if __name__ == "__main__":
    main()
