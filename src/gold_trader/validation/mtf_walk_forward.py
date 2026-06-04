"""Multi-timeframe walk-forward validation harness.

Provides :func:`validate_mtf_strategy` — runs a strategy across a list of
train/test rotations with realistic costs and returns a per-fold summary.

The harness is tagged so calling code can compare:
  - primary timeframe (15m / 60m / 240m)
  - HTF gating mode (none / follow / fade)
  - LTF entry trigger (none / displacement / engulf / structure_break)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from ..backtest import (
    BacktestSummary,
    HTFIndicatorCache,
    LTFTrigger,
    build_indicator_caches,
    make_ltf_entry_resolver,
    run_mtf_backtest,
    summarize_backtest,
)
from ..backtest.htf_indicators import build_indicator_cache
from ..backtest.engine import EntryPriceResolver
from ..data import MTFBundle, build_mtf_bundle, load_bars_from_csv
from ..models import BacktestConfig, MarketBar
from ..strategies.base import Strategy


@dataclass(frozen=True)
class FoldResult:
    fold: str
    train_lo: datetime
    train_hi: datetime
    test_lo: datetime
    test_hi: datetime
    summary: BacktestSummary
    trade_count: int
    pf: float
    avg_r: float
    win_rate: float
    net_r_per_year: float


@dataclass(frozen=True)
class MTFValidationReport:
    label: str
    primary_tf: str
    htf_codes: tuple[str, ...]
    folds: tuple[FoldResult, ...]

    @property
    def folds_with_pf_ge(self) -> int:
        return sum(1 for f in self.folds if f.pf >= 1.0)

    @property
    def folds_strong(self) -> int:
        return sum(1 for f in self.folds if f.pf >= 1.2)

    @property
    def total_trades(self) -> int:
        return sum(f.trade_count for f in self.folds)


def load_5y_ladder(
    primary_tf: str,
    htf_tfs: Iterable[str],
    *,
    data_dir: str | Path = "data/xauusd_5y",
    time_lo: datetime | None = None,
    time_hi: datetime | None = None,
) -> tuple[list[MarketBar], dict[str, list[MarketBar]]]:
    base = Path(data_dir)
    primary = load_bars_from_csv(base / f"xauusd_5y_{primary_tf}.csv")
    if time_lo or time_hi:
        primary = [
            b for b in primary
            if (time_lo is None or b.timestamp >= time_lo)
            and (time_hi is None or b.timestamp <= time_hi)
        ]
    htf_bundle: dict[str, list[MarketBar]] = {}
    for tf in htf_tfs:
        bars = load_bars_from_csv(base / f"xauusd_5y_{tf}.csv")
        if time_lo or time_hi:
            # Pad HTF window so first primary bar can have closed HTF context
            bars = [
                b for b in bars
                if (time_lo is None or b.timestamp >= time_lo - timedelta(days=10))
                and (time_hi is None or b.timestamp <= time_hi)
            ]
        htf_bundle[tf] = bars
    return primary, htf_bundle


def slice_window(
    primary: Sequence[MarketBar],
    htf_bundle: dict[str, Sequence[MarketBar]],
    lo: datetime,
    hi: datetime,
    *,
    htf_pad_days: int = 30,
) -> tuple[list[MarketBar], dict[str, list[MarketBar]]]:
    primary_slice = [b for b in primary if lo <= b.timestamp <= hi]
    htf_slice = {
        tf: [
            b for b in bars
            if lo - timedelta(days=htf_pad_days) <= b.timestamp <= hi
        ]
        for tf, bars in htf_bundle.items()
    }
    return primary_slice, htf_slice


def validate_mtf_strategy(
    label: str,
    strategy: Strategy,
    primary_tf: str,
    primary_bars: Sequence[MarketBar],
    htf_bars_by_tf: dict[str, Sequence[MarketBar]],
    splits: Sequence[tuple[str, datetime, datetime, datetime, datetime]],
    config: BacktestConfig,
    *,
    indicator_overrides: dict[str, dict] | None = None,
    ltf_bars: Sequence[MarketBar] | None = None,
    ltf_trigger: LTFTrigger | None = None,
    ltf_tf: str = "5m",
) -> MTFValidationReport:
    """Run ``strategy`` across each split and return an MTFValidationReport.

    ``splits`` is a list of ``(label, train_lo, train_hi, test_lo, test_hi)``
    tuples; only the test window is backtested (training would be where
    you tune params upstream).
    """
    folds: list[FoldResult] = []

    for split_label, train_lo, train_hi, test_lo, test_hi in splits:
        primary_slice, htf_slice = slice_window(
            primary_bars, htf_bars_by_tf, test_lo, test_hi,
        )
        if not primary_slice:
            continue
        bundle = build_mtf_bundle(primary_tf, primary_slice, htf_slice)

        # Build indicator caches with overrides if provided
        if indicator_overrides:
            indicators: dict[str, HTFIndicatorCache] = {}
            for tf in bundle.htf_codes:
                kwargs = indicator_overrides.get(tf, {})
                indicators[tf] = build_indicator_cache(bundle.htf_bars[tf], **kwargs)
        else:
            indicators = build_indicator_caches(bundle)

        resolver: EntryPriceResolver | None = None
        if ltf_bars is not None and ltf_trigger is not None:
            ltf_window = [
                b for b in ltf_bars
                if test_lo - timedelta(hours=4) <= b.timestamp <= test_hi
            ]
            if ltf_window:
                resolver = make_ltf_entry_resolver(
                    ltf_window, primary_tf=primary_tf, trigger=ltf_trigger,
                    apply_spread=False, slippage_bps=config.slippage_bps,
                )

        result = run_mtf_backtest(
            bundle, strategy, config,
            indicators=indicators,
            entry_price_resolver=resolver,
        )
        summary = summarize_backtest(result)
        days = max(1, (test_hi - test_lo).days)
        years = days / 365.25
        net_r = summary.average_r * summary.total_trades / max(0.1, years)
        folds.append(FoldResult(
            fold=split_label,
            train_lo=train_lo, train_hi=train_hi,
            test_lo=test_lo, test_hi=test_hi,
            summary=summary,
            trade_count=summary.total_trades,
            pf=summary.profit_factor if summary.profit_factor != float("inf") else 9.99,
            avg_r=summary.average_r,
            win_rate=summary.win_rate,
            net_r_per_year=net_r,
        ))

    return MTFValidationReport(
        label=label,
        primary_tf=primary_tf,
        htf_codes=tuple(htf_bars_by_tf.keys()),
        folds=tuple(folds),
    )


def format_report(rep: MTFValidationReport) -> str:
    lines: list[str] = []
    head = f"{rep.label} (primary={rep.primary_tf}, HTFs={','.join(rep.htf_codes) or 'none'})"
    lines.append(head)
    lines.append("-" * len(head))
    lines.append(f"  {'fold':>4} {'window':<23} {'n':>4} {'win%':>6} {'PF':>6} {'avgR':>7} {'netR/yr':>9}")
    for f in rep.folds:
        win = f"{f.test_lo.date()}..{f.test_hi.date()}"
        pf_str = f"{f.pf:.2f}" if f.pf < 9.99 else "  inf"
        lines.append(
            f"  {f.fold:>4} {win:<23} {f.trade_count:>4d} "
            f"{f.win_rate:>5.1%} {pf_str:>6s} {f.avg_r:>+7.3f} {f.net_r_per_year:>+9.2f}"
        )
    lines.append(
        f"  -> folds PF>=1.0: {rep.folds_with_pf_ge}/{len(rep.folds)} | "
        f"PF>=1.2: {rep.folds_strong}/{len(rep.folds)} | "
        f"total trades: {rep.total_trades}"
    )
    return "\n".join(lines)


__all__ = [
    "FoldResult",
    "MTFValidationReport",
    "load_5y_ladder",
    "slice_window",
    "validate_mtf_strategy",
    "format_report",
]
