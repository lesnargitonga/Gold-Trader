"""Conditional-probability slicer.

A strategy can fail in aggregate yet still have *slices of conditions* where
its expectancy is real. The slicer:

1. Runs the strategy across the full bar history and records every closed
   trade's outcome in R-multiples.
2. Tags each trade with regime/session/time-of-day/ATR-bucket dimensions
   evaluated at entry.
3. For each (dimension, value) bucket, reports n, win-rate, average R,
   expectancy, and a conservative lower-bound estimate.
4. Optionally enumerates 2-dim combos to surface joint conditions like
   `session=ny & vol_pct=high`.

The resulting `ProbabilityTable` is the lookup key the live agent will use
to decide whether a fresh signal is in a slice with proven edge.

No lookahead: regime tags are computed strictly from data at-or-before the
entry index, identical to the live decision path.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Sequence

from ..backtest.engine import run_backtest
from ..data.macro import MacroFrame
from ..models import BacktestConfig, ExecutedTrade, MarketBar
from ..regime import RegimeDetector
from ..strategies.base import Strategy

DEFAULT_DIMENSIONS: tuple[str, ...] = (
    "session",
    "dow",
    "hour_bucket",
    "vol_pct",
    "trend",
    "compression",
    "spread",
    "session_vwap",
    "macro_real10y",
    "macro_dxy",
    "macro_vix",
    "side",
)


@dataclass(frozen=True)
class SliceStats:
    """Stats for a single (dimension, value) or (dim1=v1 & dim2=v2) bucket."""

    key: str  # human-readable, e.g. "session=ny" or "session=ny & vol_pct=high"
    dimensions: tuple[str, ...]
    values: tuple[str, ...]
    n: int
    wins: int
    losses: int
    win_rate: float
    avg_r: float
    expectancy: float  # win_rate*avg_win_r - (1-win_rate)*avg_loss_r_abs
    avg_win_r: float
    avg_loss_r: float
    lower_ci_r: float  # avg_r - 1.96 * stderr (Gaussian approximation)
    profit_factor: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProbabilityTable:
    """Full conditional-probability report for one strategy."""

    family: str
    n_total: int
    base_win_rate: float
    base_avg_r: float
    base_expectancy: float
    base_profit_factor: float
    single_slices: tuple[SliceStats, ...]
    pair_slices: tuple[SliceStats, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "family": self.family,
            "n_total": self.n_total,
            "base_win_rate": self.base_win_rate,
            "base_avg_r": self.base_avg_r,
            "base_expectancy": self.base_expectancy,
            "base_profit_factor": self.base_profit_factor,
            "single_slices": [s.to_dict() for s in self.single_slices],
            "pair_slices": [s.to_dict() for s in self.pair_slices],
        }

    def edge_slices(
        self,
        *,
        min_n: int = 20,
        min_expectancy_r: float = 0.10,
        min_profit_factor: float = 1.20,
        require_lower_ci_positive: bool = True,
    ) -> tuple[SliceStats, ...]:
        """Return slices that pass all probability gates."""
        out: list[SliceStats] = []
        for s in (*self.single_slices, *self.pair_slices):
            if s.n < min_n:
                continue
            if s.expectancy < min_expectancy_r:
                continue
            if s.profit_factor < min_profit_factor:
                continue
            if require_lower_ci_positive and s.lower_ci_r <= 0:
                continue
            out.append(s)
        out.sort(key=lambda s: s.expectancy * math.sqrt(s.n), reverse=True)
        return tuple(out)


# ---------------------------------------------------------------------------
# Trade tagging
# ---------------------------------------------------------------------------


def _hour_bucket(ts: datetime) -> str:
    h = ts.hour
    if 0 <= h < 6:
        return "00-06"
    if 6 <= h < 12:
        return "06-12"
    if 12 <= h < 18:
        return "12-18"
    return "18-24"


def _dow_label(ts: datetime) -> str:
    return ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[ts.weekday()]


def _index_at_time(bars: Sequence[MarketBar], ts: datetime) -> int | None:
    # Trades store entry_time = bar[entry_index].timestamp; binary search.
    lo, hi = 0, len(bars) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        bt = bars[mid].timestamp
        if bt == ts:
            return mid
        if bt < ts:
            lo = mid + 1
        else:
            hi = mid - 1
    return None


def _trade_dimensions(
    trade: ExecutedTrade,
    bars: Sequence[MarketBar],
    detector: RegimeDetector,
    macro: MacroFrame | None,
) -> dict[str, str]:
    idx = _index_at_time(bars, trade.entry_time)
    if idx is None or idx < 1:
        return {}
    tags = detector.classify(bars, idx, macro=macro)
    bar = bars[idx]
    return {
        "session": bar.session,
        "dow": _dow_label(trade.entry_time),
        "hour_bucket": _hour_bucket(trade.entry_time),
        "vol_pct": tags.vol_pct,
        "trend": tags.trend,
        "compression": tags.compression,
        "spread": tags.spread,
        "session_vwap": tags.session_vwap,
        "macro_real10y": tags.macro_real10y,
        "macro_dxy": tags.macro_dxy,
        "macro_vix": tags.macro_vix,
        "side": trade.side.value,
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _stats(
    key: str,
    dimensions: tuple[str, ...],
    values: tuple[str, ...],
    r_multiples: list[float],
) -> SliceStats:
    n = len(r_multiples)
    wins_r = [r for r in r_multiples if r > 0]
    losses_r = [r for r in r_multiples if r <= 0]
    wins = len(wins_r)
    losses = len(losses_r)
    win_rate = wins / n if n else 0.0
    avg_r = sum(r_multiples) / n if n else 0.0
    avg_win_r = sum(wins_r) / wins if wins else 0.0
    avg_loss_r = sum(losses_r) / losses if losses else 0.0  # negative or zero
    expectancy = win_rate * avg_win_r + (1 - win_rate) * avg_loss_r
    if n > 1:
        mean = avg_r
        var = sum((r - mean) ** 2 for r in r_multiples) / (n - 1)
        stderr = math.sqrt(var / n)
    else:
        stderr = 0.0
    lower_ci_r = avg_r - 1.96 * stderr
    gross_win = sum(wins_r)
    gross_loss = -sum(losses_r)
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
    return SliceStats(
        key=key,
        dimensions=dimensions,
        values=values,
        n=n,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        avg_r=avg_r,
        expectancy=expectancy,
        avg_win_r=avg_win_r,
        avg_loss_r=avg_loss_r,
        lower_ci_r=lower_ci_r,
        profit_factor=profit_factor,
    )


def compute_probability_table(
    bars: Sequence[MarketBar],
    strategy: Strategy,
    config: BacktestConfig,
    *,
    family: str | None = None,
    macro: MacroFrame | None = None,
    detector: RegimeDetector | None = None,
    dimensions: Sequence[str] = DEFAULT_DIMENSIONS,
    include_pairs: bool = True,
    min_pair_n: int = 8,
) -> ProbabilityTable:
    """Run *strategy* across *bars* and slice the resulting trades."""

    detector = detector or RegimeDetector()
    family = family or strategy.name

    result = run_backtest(bars, strategy, config)
    trades = list(result.trades)

    # tag every trade
    tagged: list[tuple[float, dict[str, str]]] = []
    for trade in trades:
        dims = _trade_dimensions(trade, bars, detector, macro)
        if not dims:
            continue
        tagged.append((trade.pnl_r, dims))

    return _build_table_from_tagged(
        tagged, family,
        dimensions=dimensions,
        include_pairs=include_pairs,
        min_pair_n=min_pair_n,
    )


def compute_pooled_probability_table(
    bars: Sequence[MarketBar],
    strategies: Sequence[Strategy],
    config: BacktestConfig,
    *,
    family: str,
    macro: MacroFrame | None = None,
    detector: RegimeDetector | None = None,
    dimensions: Sequence[str] = DEFAULT_DIMENSIONS,
    include_pairs: bool = True,
    min_pair_n: int = 8,
) -> ProbabilityTable:
    """Pool trades across multiple strategy variants (e.g. an entire grid).

    Each variant of the same family conditions on the same regime dimensions;
    pooling grows n without diluting the signal because all variants share
    the rule's structural identity. Useful when a single param set produces
    too few trades to populate slice tables.

    To avoid the same physical signal being counted 1,728× (once per grid
    combo), trades sharing the same ``(entry_time, side)`` are collapsed
    into a single tagged sample whose ``pnl_r`` is the **mean** R across the
    variants that triggered there. This represents the expected R for that
    signal under a random draw from the grid, which is the methodologically
    honest summary.
    """
    detector = detector or RegimeDetector()
    # collect all trades, grouped by (entry_time, side)
    grouped: dict[tuple, list[float]] = {}
    grouped_dims: dict[tuple, dict[str, str]] = {}
    for strategy in strategies:
        result = run_backtest(bars, strategy, config)
        for trade in result.trades:
            dims = _trade_dimensions(trade, bars, detector, macro)
            if not dims:
                continue
            key = (trade.entry_time, trade.side)
            grouped.setdefault(key, []).append(trade.pnl_r)
            # dims depend only on regime at entry, identical across variants
            grouped_dims.setdefault(key, dims)
    tagged: list[tuple[float, dict[str, str]]] = [
        (sum(rs) / len(rs), grouped_dims[key])
        for key, rs in grouped.items()
    ]
    return _build_table_from_tagged(
        tagged, family,
        dimensions=dimensions,
        include_pairs=include_pairs,
        min_pair_n=min_pair_n,
    )


def _build_table_from_tagged(
    tagged: list[tuple[float, dict[str, str]]],
    family: str,
    *,
    dimensions: Sequence[str],
    include_pairs: bool,
    min_pair_n: int,
) -> ProbabilityTable:
    n_total = len(tagged)
    if n_total == 0:
        return ProbabilityTable(
            family=family,
            n_total=0,
            base_win_rate=0.0,
            base_avg_r=0.0,
            base_expectancy=0.0,
            base_profit_factor=0.0,
            single_slices=(),
            pair_slices=(),
        )

    base = _stats("base", ("base",), ("all",), [r for r, _ in tagged])

    # single dim
    single: list[SliceStats] = []
    for dim in dimensions:
        groups: dict[str, list[float]] = {}
        for r, dims in tagged:
            v = dims.get(dim)
            if v is None:
                continue
            groups.setdefault(v, []).append(r)
        for value, rs in groups.items():
            if len(rs) < 2:  # never report singletons
                continue
            single.append(_stats(f"{dim}={value}", (dim,), (value,), rs))

    pair: list[SliceStats] = []
    if include_pairs:
        for d1, d2 in combinations(dimensions, 2):
            groups: dict[tuple[str, str], list[float]] = {}
            for r, dims in tagged:
                v1, v2 = dims.get(d1), dims.get(d2)
                if v1 is None or v2 is None:
                    continue
                groups.setdefault((v1, v2), []).append(r)
            for (v1, v2), rs in groups.items():
                if len(rs) < min_pair_n:
                    continue
                pair.append(
                    _stats(
                        f"{d1}={v1} & {d2}={v2}",
                        (d1, d2),
                        (v1, v2),
                        rs,
                    )
                )

    single.sort(key=lambda s: s.expectancy * math.sqrt(s.n), reverse=True)
    pair.sort(key=lambda s: s.expectancy * math.sqrt(s.n), reverse=True)

    return ProbabilityTable(
        family=family,
        n_total=n_total,
        base_win_rate=base.win_rate,
        base_avg_r=base.avg_r,
        base_expectancy=base.expectancy,
        base_profit_factor=base.profit_factor,
        single_slices=tuple(single),
        pair_slices=tuple(pair),
    )


# ---------------------------------------------------------------------------
# Persistence + lookup
# ---------------------------------------------------------------------------


def write_probability_table(table: ProbabilityTable, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(table.to_dict(), indent=2, default=str))
    return p


def lookup_slice_probability(
    table: ProbabilityTable,
    current_dims: dict[str, str],
    *,
    min_n: int = 20,
    min_expectancy_r: float = 0.10,
    min_profit_factor: float = 1.20,
) -> SliceStats | None:
    """Find the most-specific edge slice that matches the current regime.

    Pair slices are preferred over single-dim slices when both qualify; ties
    broken by higher `expectancy * sqrt(n)`.
    """
    candidates: list[SliceStats] = []
    for s in (*table.pair_slices, *table.single_slices):
        if s.n < min_n:
            continue
        if s.expectancy < min_expectancy_r:
            continue
        if s.profit_factor < min_profit_factor:
            continue
        if all(current_dims.get(d) == v for d, v in zip(s.dimensions, s.values)):
            candidates.append(s)
    if not candidates:
        return None
    # prefer 2-dim over 1-dim, then higher expectancy*sqrt(n)
    candidates.sort(
        key=lambda s: (len(s.dimensions), s.expectancy * math.sqrt(s.n)),
        reverse=True,
    )
    return candidates[0]


__all__ = [
    "DEFAULT_DIMENSIONS",
    "ProbabilityTable",
    "SliceStats",
    "compute_probability_table",
    "lookup_slice_probability",
    "write_probability_table",
]
