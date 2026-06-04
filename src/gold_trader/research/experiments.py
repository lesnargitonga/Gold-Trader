from __future__ import annotations

import os
import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Iterable, Sequence

from ..backtest import BacktestSummary, summarize_backtest
from ..backtest.engine import run_backtest
from ..data import load_bars_from_csv
from ..models import BacktestConfig, MarketBar
from ..strategies import (
    AsianRangeBreakoutStrategy,
    CompressionBreakoutStrategy,
    LiquiditySweepStrategy,
    LondonBreakoutStrategy,
    MomentumBurstStrategy,
    NYSessionBreakoutStrategy,
    TrendPullbackStrategy,
)
from ..validation import WalkForwardAggregate, run_walk_forward, summarize_walk_forward
from .sweep import LiquiditySweepParameters, build_liquidity_sweep_grid


@dataclass(frozen=True)
class AsianRangeBreakoutParameters:
    atr_period: int
    risk_reward: float
    max_spread: float
    min_breakout_atr: float
    min_range_atr: float
    min_asian_bars: int
    min_atr_threshold: float = 0.0   # volatility regime filter
    min_risk_atr: float = 0.0        # reject if stop_dist < min_risk_atr × ATR; 0.0 = disabled


@dataclass(frozen=True)
class NYSessionBreakoutParameters:
    atr_period: int
    risk_reward: float
    max_spread: float
    min_breakout_atr: float
    min_range_atr: float
    min_london_bars: int
    entry_end_hour: int = 21         # restrict NY entries to before this UTC hour
    require_asian_alignment: bool = False  # Asian direction must align with signal


@dataclass(frozen=True)
class MomentumBurstParameters:
    atr_period: int
    min_body_atr: float
    body_fraction: float
    risk_reward: float
    max_spread: float
    min_atr_threshold: float = 0.0


@dataclass(frozen=True)
class LondonBreakoutParameters:
    opening_range_bars: int
    atr_period: int
    min_breakout_atr: float
    risk_reward: float
    max_spread: float
    min_atr_threshold: float = 0.0


@dataclass(frozen=True)
class TrendPullbackParameters:
    ema_fast: int
    ema_slow: int
    atr_period: int
    trend_strength_min: float
    pullback_tolerance: float
    risk_reward: float
    max_spread: float


@dataclass(frozen=True)
class CompressionBreakoutParameters:
    breakout_lookback: int
    compression_lookback: int
    atr_period: int
    max_compression_atr_ratio: float
    min_breakout_atr: float
    risk_reward: float
    max_spread: float
    min_news_distance_minutes: float
    min_atr_threshold: float = 0.0


@dataclass(frozen=True)
class ResearchResult:
    family: str
    timeframe_minutes: int
    parameter_text: str
    summary: BacktestSummary
    walk_forward: WalkForwardAggregate


def build_asian_range_breakout_grid(
    atr_periods: Iterable[int],
    risk_rewards: Iterable[float],
    max_spreads: Iterable[float],
    min_breakout_atrs: Iterable[float],
    min_range_atrs: Iterable[float],
    min_asian_bars_list: Iterable[int],
    min_atr_thresholds: Iterable[float] = (0.0,),
) -> list[AsianRangeBreakoutParameters]:
    grid: list[AsianRangeBreakoutParameters] = []
    for atr_period in tuple(atr_periods):
        for risk_reward in tuple(risk_rewards):
            for max_spread in tuple(max_spreads):
                for min_breakout_atr in tuple(min_breakout_atrs):
                    for min_range_atr in tuple(min_range_atrs):
                        for min_asian_bars in tuple(min_asian_bars_list):
                            for min_atr_threshold in tuple(min_atr_thresholds):
                                grid.append(
                                    AsianRangeBreakoutParameters(
                                        atr_period=atr_period,
                                        risk_reward=risk_reward,
                                        max_spread=max_spread,
                                        min_breakout_atr=min_breakout_atr,
                                        min_range_atr=min_range_atr,
                                        min_asian_bars=min_asian_bars,
                                        min_atr_threshold=min_atr_threshold,
                                    )
                                )
    if not grid:
        raise ValueError("asian range breakout parameter grid is empty")
    return grid


def build_compression_breakout_grid(
    breakout_lookbacks: Iterable[int],
    compression_lookbacks: Iterable[int],
    atr_periods: Iterable[int],
    max_compression_atr_ratios: Iterable[float],
    min_breakout_atrs: Iterable[float],
    risk_rewards: Iterable[float],
    max_spreads: Iterable[float],
    min_news_distances: Iterable[float],
    min_atr_thresholds: Iterable[float] = (0.0,),
) -> list[CompressionBreakoutParameters]:
    grid: list[CompressionBreakoutParameters] = []
    for breakout_lookback in tuple(breakout_lookbacks):
        for compression_lookback in tuple(compression_lookbacks):
            for atr_period in tuple(atr_periods):
                for compression_ratio in tuple(max_compression_atr_ratios):
                    for min_breakout_atr in tuple(min_breakout_atrs):
                        for risk_reward in tuple(risk_rewards):
                            for max_spread in tuple(max_spreads):
                                for min_news_distance in tuple(min_news_distances):
                                    for min_atr_threshold in tuple(min_atr_thresholds):
                                        grid.append(
                                            CompressionBreakoutParameters(
                                                breakout_lookback=breakout_lookback,
                                                compression_lookback=compression_lookback,
                                                atr_period=atr_period,
                                                max_compression_atr_ratio=compression_ratio,
                                                min_breakout_atr=min_breakout_atr,
                                                risk_reward=risk_reward,
                                                max_spread=max_spread,
                                                min_news_distance_minutes=min_news_distance,
                                                min_atr_threshold=min_atr_threshold,
                                            )
                                        )
    if not grid:
        raise ValueError("compression breakout parameter grid is empty")
    return grid


def load_timeframe_bundle(directory: str | Path, timeframes: Sequence[int]) -> dict[int, list[MarketBar]]:
    bundle_dir = Path(directory)
    files_by_timeframe: dict[int, list[Path]] = {}
    timeframe_pattern = re.compile(r"_(\d+)m\.csv$")

    for path in bundle_dir.glob("*.csv"):
        match = timeframe_pattern.search(path.name)
        if not match:
            continue
        timeframe_minutes = int(match.group(1))
        files_by_timeframe.setdefault(timeframe_minutes, []).append(path)

    datasets: dict[int, list[MarketBar]] = {}
    for timeframe_minutes in timeframes:
        candidates = files_by_timeframe.get(timeframe_minutes, [])
        if not candidates:
            continue
        newest = max(candidates, key=lambda candidate: candidate.stat().st_mtime)
        datasets[timeframe_minutes] = load_bars_from_csv(newest)

    return datasets


def run_research_bundle(
    datasets: dict[int, Sequence[MarketBar]],
    config: BacktestConfig,
    families: Sequence[str],
    liquidity_grid: Sequence[LiquiditySweepParameters],
    compression_grid: Sequence[CompressionBreakoutParameters],
    train_bars: int,
    test_bars: int,
    step_bars: int | None,
    min_trades: int,
    max_workers: int = 1,
    asian_range_grid: Sequence[AsianRangeBreakoutParameters] | None = None,
    london_breakout_grid: Sequence[LondonBreakoutParameters] | None = None,
    trend_pullback_grid: Sequence[TrendPullbackParameters] | None = None,
    ny_session_breakout_grid: Sequence[NYSessionBreakoutParameters] | None = None,
    momentum_burst_grid: Sequence[MomentumBurstParameters] | None = None,
) -> list[ResearchResult]:
    tasks = _build_tasks(
        datasets=datasets,
        families=families,
        liquidity_grid=liquidity_grid,
        compression_grid=compression_grid,
        asian_range_grid=asian_range_grid or [],
        london_breakout_grid=london_breakout_grid or [],
        trend_pullback_grid=trend_pullback_grid or [],
        ny_session_breakout_grid=ny_session_breakout_grid or [],
        momentum_burst_grid=momentum_burst_grid or [],
        config=config,
        train_bars=train_bars,
        test_bars=test_bars,
        step_bars=step_bars,
        min_trades=min_trades,
    )
    if not tasks:
        return []

    resolved_workers = _resolve_workers(max_workers, len(tasks))
    if resolved_workers == 1:
        results = [_evaluate_task(task) for task in tasks]
    else:
        with ProcessPoolExecutor(max_workers=resolved_workers) as executor:
            results = list(executor.map(_evaluate_task, tasks))

    filtered = [result for result in results if result is not None]
    return sorted(filtered, key=_sort_key, reverse=True)


def build_london_breakout_grid(
    opening_range_bars_list: Iterable[int],
    atr_periods: Iterable[int],
    min_breakout_atrs: Iterable[float],
    risk_rewards: Iterable[float],
    max_spreads: Iterable[float],
    min_atr_thresholds: Iterable[float] = (0.0,),
) -> list[LondonBreakoutParameters]:
    grid: list[LondonBreakoutParameters] = []
    for orb in tuple(opening_range_bars_list):
        for atr_period in tuple(atr_periods):
            for min_ba in tuple(min_breakout_atrs):
                for rr in tuple(risk_rewards):
                    for ms in tuple(max_spreads):
                        for mat in tuple(min_atr_thresholds):
                            grid.append(
                                LondonBreakoutParameters(
                                    opening_range_bars=orb,
                                    atr_period=atr_period,
                                    min_breakout_atr=min_ba,
                                    risk_reward=rr,
                                    max_spread=ms,
                                    min_atr_threshold=mat,
                                )
                            )
    if not grid:
        raise ValueError("london breakout parameter grid is empty")
    return grid


def build_trend_pullback_grid(
    ema_fasts: Iterable[int],
    ema_slows: Iterable[int],
    atr_periods: Iterable[int],
    trend_strengths: Iterable[float],
    pullback_tolerances: Iterable[float],
    risk_rewards: Iterable[float],
    max_spreads: Iterable[float],
) -> list[TrendPullbackParameters]:
    grid: list[TrendPullbackParameters] = []
    for ef in tuple(ema_fasts):
        for es in tuple(ema_slows):
            if es <= ef:
                continue
            for ap in tuple(atr_periods):
                for ts in tuple(trend_strengths):
                    for pt in tuple(pullback_tolerances):
                        for rr in tuple(risk_rewards):
                            for ms in tuple(max_spreads):
                                grid.append(
                                    TrendPullbackParameters(
                                        ema_fast=ef,
                                        ema_slow=es,
                                        atr_period=ap,
                                        trend_strength_min=ts,
                                        pullback_tolerance=pt,
                                        risk_reward=rr,
                                        max_spread=ms,
                                    )
                                )
    if not grid:
        raise ValueError("trend pullback parameter grid is empty")
    return grid


def build_ny_session_breakout_grid(
    atr_periods: Iterable[int],
    risk_rewards: Iterable[float],
    max_spreads: Iterable[float],
    min_breakout_atrs: Iterable[float],
    min_range_atrs: Iterable[float],
    min_london_bars_list: Iterable[int],
    entry_end_hours: Iterable[int] = (21,),
    require_asian_alignments: Iterable[bool] = (False,),
) -> list[NYSessionBreakoutParameters]:
    grid: list[NYSessionBreakoutParameters] = []
    for ap in tuple(atr_periods):
        for rr in tuple(risk_rewards):
            for ms in tuple(max_spreads):
                for mba in tuple(min_breakout_atrs):
                    for mra in tuple(min_range_atrs):
                        for mlb in tuple(min_london_bars_list):
                            for eeh in tuple(entry_end_hours):
                                for raa in tuple(require_asian_alignments):
                                    grid.append(
                                        NYSessionBreakoutParameters(
                                            atr_period=ap,
                                            risk_reward=rr,
                                            max_spread=ms,
                                            min_breakout_atr=mba,
                                            min_range_atr=mra,
                                            min_london_bars=mlb,
                                            entry_end_hour=eeh,
                                            require_asian_alignment=raa,
                                        )
                                    )
    if not grid:
        raise ValueError("ny session breakout parameter grid is empty")
    return grid


def build_momentum_burst_grid(
    atr_periods: Iterable[int],
    min_body_atrs: Iterable[float],
    body_fractions: Iterable[float],
    risk_rewards: Iterable[float],
    max_spreads: Iterable[float],
    min_atr_thresholds: Iterable[float] = (0.0,),
) -> list[MomentumBurstParameters]:
    grid: list[MomentumBurstParameters] = []
    for ap in tuple(atr_periods):
        for mb in tuple(min_body_atrs):
            for bf in tuple(body_fractions):
                for rr in tuple(risk_rewards):
                    for ms in tuple(max_spreads):
                        for mat in tuple(min_atr_thresholds):
                            grid.append(
                                MomentumBurstParameters(
                                    atr_period=ap,
                                    min_body_atr=mb,
                                    body_fraction=bf,
                                    risk_reward=rr,
                                    max_spread=ms,
                                    min_atr_threshold=mat,
                                )
                            )
    if not grid:
        raise ValueError("momentum burst parameter grid is empty")
    return grid


def default_ny_session_breakout_grid() -> list[NYSessionBreakoutParameters]:
    return build_ny_session_breakout_grid(
        atr_periods=[10, 14, 20],
        risk_rewards=[1.5, 2.0, 2.5],
        max_spreads=[0.75, 1.00],
        min_breakout_atrs=[0.05, 0.10, 0.20],
        min_range_atrs=[0.30, 0.50, 0.80],
        min_london_bars_list=[3, 4, 6],
        entry_end_hours=[15, 17, 21],
        require_asian_alignments=[False, True],
    )


def default_momentum_burst_grid() -> list[MomentumBurstParameters]:
    return build_momentum_burst_grid(
        atr_periods=[10, 14, 20],
        min_body_atrs=[0.40, 0.60, 0.80],
        body_fractions=[0.15, 0.25, 0.35],
        risk_rewards=[1.5, 2.0, 2.5],
        max_spreads=[0.75, 1.00],
        min_atr_thresholds=[0.0, 5.0, 10.0],
    )


def default_london_breakout_grid() -> list[LondonBreakoutParameters]:
    return build_london_breakout_grid(
        opening_range_bars_list=[2, 4, 6],
        atr_periods=[10, 14, 20],
        min_breakout_atrs=[0.05, 0.10, 0.20],
        risk_rewards=[1.5, 2.0, 2.5],
        max_spreads=[0.75, 1.00],
        min_atr_thresholds=[0.0, 5.0, 10.0],
    )


def default_trend_pullback_grid() -> list[TrendPullbackParameters]:
    return build_trend_pullback_grid(
        ema_fasts=[10, 20],
        ema_slows=[50, 100],
        atr_periods=[10, 14],
        trend_strengths=[0.5, 0.8, 1.2],
        pullback_tolerances=[0.2, 0.4, 0.6],
        risk_rewards=[1.5, 2.0, 2.5],
        max_spreads=[0.75, 1.00],
    )


def default_asian_range_grid() -> list[AsianRangeBreakoutParameters]:
    return build_asian_range_breakout_grid(
        atr_periods=[10, 14, 20],
        risk_rewards=[1.5, 2.0, 2.5],
        max_spreads=[0.75, 1.00, 1.25],
        min_breakout_atrs=[0.03, 0.05, 0.10],
        min_range_atrs=[0.20, 0.30, 0.50],
        min_asian_bars_list=[3, 4, 6],
        min_atr_thresholds=[0.0, 5.0, 10.0],
    )


def default_liquidity_grid() -> list[LiquiditySweepParameters]:
    return build_liquidity_sweep_grid(
        lookbacks=[10, 15, 20, 30],
        atr_periods=[10, 14, 20],
        min_sweep_atrs=[0.1, 0.2, 0.3],
        risk_rewards=[1.5, 2.0, 2.5],
        max_spreads=[0.50, 0.75, 1.00],
        min_news_distances=[0.0, 30.0],
    )


def default_compression_grid() -> list[CompressionBreakoutParameters]:
    return build_compression_breakout_grid(
        breakout_lookbacks=[8, 12, 16],
        compression_lookbacks=[4, 6, 8],
        atr_periods=[10, 14],
        max_compression_atr_ratios=[1.0, 1.5, 2.0, 2.5, 3.0],
        min_breakout_atrs=[0.05, 0.10, 0.15],
        risk_rewards=[1.5, 2.0, 2.5],
        max_spreads=[0.75, 1.00, 1.25],
        min_news_distances=[0.0, 30.0],
        min_atr_thresholds=[0.0, 5.0, 10.0],
    )


# ── New strategy parameter types and grids ─────────────────────────────────


@dataclass(frozen=True)
class PreviousDayBreakoutParameters:
    atr_period: int
    risk_reward: float
    max_spread: float
    min_breakout_atr: float
    stop_atr_buffer: float


@dataclass(frozen=True)
class OpeningRangeBreakoutParameters:
    opening_range_bars: int
    atr_period: int
    min_breakout_atr: float
    risk_reward: float
    max_spread: float


@dataclass(frozen=True)
class AsianRangeFadeParameters:
    atr_period: int
    risk_reward: float
    max_spread: float
    min_rejection_atr: float
    min_range_atr: float
    stop_atr_buffer: float


def default_previous_day_breakout_grid() -> list[PreviousDayBreakoutParameters]:
    grid: list[PreviousDayBreakoutParameters] = []
    for atr in [10, 14, 20]:
        for rr in [1.5, 2.0, 2.5]:
            for spread in [0.75, 1.00, 1.25]:
                for min_ext in [0.03, 0.05, 0.10]:
                    for stop_buf in [0.3, 0.5, 0.8]:
                        grid.append(PreviousDayBreakoutParameters(
                            atr_period=atr,
                            risk_reward=rr,
                            max_spread=spread,
                            min_breakout_atr=min_ext,
                            stop_atr_buffer=stop_buf,
                        ))
    return grid


def default_opening_range_breakout_grid() -> list[OpeningRangeBreakoutParameters]:
    grid: list[OpeningRangeBreakoutParameters] = []
    for orb_bars in [2, 4, 6]:       # 30m, 60m, 90m opening range
        for atr in [10, 14, 20]:
            for min_ext in [0.05, 0.10, 0.15]:
                for rr in [1.5, 2.0, 2.5]:
                    for spread in [0.75, 1.00, 1.25]:
                        grid.append(OpeningRangeBreakoutParameters(
                            opening_range_bars=orb_bars,
                            atr_period=atr,
                            min_breakout_atr=min_ext,
                            risk_reward=rr,
                            max_spread=spread,
                        ))
    return grid


def default_asian_range_fade_grid() -> list[AsianRangeFadeParameters]:
    grid: list[AsianRangeFadeParameters] = []
    for atr in [10, 14, 20]:
        for rr in [1.2, 1.5, 2.0]:
            for spread in [0.75, 1.00, 1.25]:
                for min_rej in [0.10, 0.15, 0.25]:
                    for min_rng in [0.20, 0.30]:
                        for stop_buf in [0.2, 0.3, 0.5]:
                            grid.append(AsianRangeFadeParameters(
                                atr_period=atr,
                                risk_reward=rr,
                                max_spread=spread,
                                min_rejection_atr=min_rej,
                                min_range_atr=min_rng,
                                stop_atr_buffer=stop_buf,
                            ))
    return grid


# ── Session Continuation ────────────────────────────────────────────────────

@dataclass(frozen=True)
class SessionContinuationParameters:
    atr_period: int
    risk_reward: float
    max_spread: float
    min_session_quantile: float
    min_range_atr: float
    entry_slippage_buffer: float


def default_session_continuation_grid() -> list[SessionContinuationParameters]:
    grid: list[SessionContinuationParameters] = []
    for atr in [10, 14, 20]:
        for rr in [1.2, 1.5, 2.0]:
            for spread in [0.75, 1.00, 1.25]:
                for quantile in [0.55, 0.65, 0.75]:
                    for min_rng in [0.20, 0.30, 0.50]:
                        for slippage in [0.05, 0.10, 0.20]:
                            grid.append(SessionContinuationParameters(
                                atr_period=atr,
                                risk_reward=rr,
                                max_spread=spread,
                                min_session_quantile=quantile,
                                min_range_atr=min_rng,
                                entry_slippage_buffer=slippage,
                            ))
    return grid


# ── DXY Lead-Lag ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DXYLeadLagParameters:
    lookback: int
    min_dxy_drop: float
    max_gold_response: float
    atr_period: int
    stop_atr_mult: float
    risk_reward: float
    max_spread: float
    min_atr_threshold: float


def default_dxy_lead_lag_grid() -> list[DXYLeadLagParameters]:
    grid: list[DXYLeadLagParameters] = []
    for lookback in [1, 2, 3, 5]:
        for min_drop in [0.001, 0.002, 0.003, 0.005]:
            for max_resp in [0.30, 0.50, 0.70]:
                for atr in [14, 20]:
                    for rr in [1.5, 2.0, 2.5]:
                        grid.append(DXYLeadLagParameters(
                            lookback=lookback,
                            min_dxy_drop=min_drop,
                            max_gold_response=max_resp,
                            atr_period=atr,
                            stop_atr_mult=1.0,
                            risk_reward=rr,
                            max_spread=1.00,
                            min_atr_threshold=0.0,
                        ))
    return grid


# ── Real-Yield Reversal ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class RealYieldReversalParameters:
    yield_lookback_days: int
    min_yield_move_bps: float
    atr_period: int
    stop_atr_mult: float
    risk_reward: float
    enter_longs: bool = True
    enter_shorts: bool = True
    max_spread: float = 1.00
    min_atr_threshold: float = 0.0


def default_real_yield_reversal_grid() -> list[RealYieldReversalParameters]:
    """Compact grid calibrated to the empirical |Δreal10y| distribution.

    Real-10Y daily volatility is ~3 bps; p95 of 5-day moves is ~15 bps.  Floors
    above 10 bps fire too rarely on a 15-month dataset (<20 events).  The grid
    below produces 30-100 train events per row, which is the sweet spot.

    4 lookbacks × 4 thresholds × 2 ATRs × 2 stops × 3 RR × 3 directional modes = 576 combos.
    """
    grid: list[RealYieldReversalParameters] = []
    direction_modes = [
        (True, True),    # both
        (True, False),   # longs only (yield-drops bullish for gold)
        (False, True),   # shorts only (yield-spikes bearish for gold)
    ]
    for lookback in [5, 7, 10, 14]:
        for thr_bps in [3.0, 5.0, 7.0, 10.0]:
            for atr in [14, 20]:
                for stop_mult in [1.0, 1.5]:
                    for rr in [1.5, 2.0, 2.5]:
                        for el, es in direction_modes:
                            grid.append(RealYieldReversalParameters(
                                yield_lookback_days=lookback,
                                min_yield_move_bps=thr_bps,
                                atr_period=atr,
                                stop_atr_mult=stop_mult,
                                risk_reward=rr,
                                enter_longs=el,
                                enter_shorts=es,
                            ))
    return grid


# ── Timed-Horizon Macro Regime ──────────────────────────────────────────────

@dataclass(frozen=True)
class TimedHorizonMacroRegimeParameters:
    """Parameters for the Phase-14b PREMIUM macro construct.

    The validated cell is

        real_yield_lookback_days=10, real_yield_max_change_bps=0.0,
        vix_max_change_abs=2.5, dxy_max_abs_change_pct=1.0, far_atr_mult=8.0

    See /memories/repo/notes.md (2026-05-10 PHASE 14b) for verification:
    8/8 covered quarters positive, p < 1e-3 Bonferroni-corrected, 60m + 240m
    cross-TF replication.  15m correctly fails.
    """
    real_yield_lookback_days: int
    real_yield_max_change_bps: float
    vix_max_change_abs: float
    dxy_max_abs_change_pct: float
    far_atr_mult: float
    vix_lookback_days: int = 5
    dxy_lookback_days: int = 20
    atr_period: int = 14
    once_per_day: bool = True
    require_dxy_flat: bool = True
    require_bullish_close: bool = False
    max_spread: float = 1.50


def default_timed_horizon_macro_regime_grid() -> list[TimedHorizonMacroRegimeParameters]:
    """Same 48-cell grid used in scripts/macro_premium_audit.py.

    The validated survivor is one cell of this grid; keeping the surrounding
    cells makes weekly-champion re-fits stable to small regime drift.
    """
    grid: list[TimedHorizonMacroRegimeParameters] = []
    for ry_lookback in [5, 10]:
        for ry_max_bps in [-3.0, 0.0]:
            for vix_max in [1.5, 2.5]:
                for dxy_pct in [0.5, 1.0]:
                    for far in [8.0, 12.0, 16.0]:
                        grid.append(TimedHorizonMacroRegimeParameters(
                            real_yield_lookback_days=ry_lookback,
                            real_yield_max_change_bps=ry_max_bps,
                            vix_max_change_abs=vix_max,
                            dxy_max_abs_change_pct=dxy_pct,
                            far_atr_mult=far,
                        ))
    return grid


# ── Fair Value Gap ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FairValueGapParameters:
    atr_period: int
    risk_reward: float
    max_spread: float
    min_gap_atr: float
    fvg_lookback: int
    stop_buffer_atr: float


def default_fair_value_gap_grid() -> list[FairValueGapParameters]:
    grid: list[FairValueGapParameters] = []
    for atr in [10, 14, 20]:
        for rr in [1.5, 2.0, 2.5]:
            for spread in [0.75, 1.00, 1.25]:
                for min_gap in [0.03, 0.05, 0.10]:
                    for lookback in [10, 20, 30]:
                        for stop_buf in [0.05, 0.10, 0.20]:
                            grid.append(FairValueGapParameters(
                                atr_period=atr,
                                risk_reward=rr,
                                max_spread=spread,
                                min_gap_atr=min_gap,
                                fvg_lookback=lookback,
                                stop_buffer_atr=stop_buf,
                            ))
    return grid


# ── Inversion Fair Value Gap ───────────────────────────────────────────────

@dataclass(frozen=True)
class InversionFairValueGapParameters:
    atr_period: int
    risk_reward: float
    max_spread: float
    min_gap_atr: float
    fvg_lookback: int
    inversion_lookback: int
    retest_lookback: int
    stop_buffer_atr: float


def default_inversion_fair_value_gap_grid() -> list[InversionFairValueGapParameters]:
    grid: list[InversionFairValueGapParameters] = []
    for atr in [10, 14, 20]:
        for rr in [1.5, 2.0, 2.5]:
            for spread in [0.75, 1.00]:
                for min_gap in [0.05, 0.10, 0.20]:
                    for lookback in [20, 30]:
                        for inv_lb in [10, 20]:
                            for retest_lb in [5, 10]:
                                for stop_buf in [0.10, 0.20]:
                                    grid.append(InversionFairValueGapParameters(
                                        atr_period=atr,
                                        risk_reward=rr,
                                        max_spread=spread,
                                        min_gap_atr=min_gap,
                                        fvg_lookback=lookback,
                                        inversion_lookback=inv_lb,
                                        retest_lookback=retest_lb,
                                        stop_buffer_atr=stop_buf,
                                    ))
    return grid


# ── RSI Divergence ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RsiDivergenceParameters:
    rsi_period: int
    atr_period: int
    risk_reward: float
    max_spread: float
    overbought: float
    oversold: float
    pivot_window: int
    pivot_lookback: int
    min_pivot_separation: int
    stop_buffer_atr: float


def default_rsi_divergence_grid() -> list[RsiDivergenceParameters]:
    """RSI divergence parameter grid.

    2026-05-09 — widened to grow holdout n from ~6 toward ≥30.
    Looser thresholds (oversold up to 35), shorter pivot separations (down to 3),
    and longer pivot lookbacks (up to 60) trade some PF magnitude for sample size.
    """
    grid: list[RsiDivergenceParameters] = []
    for rsi in [10, 14]:
        for atr in [10, 14]:
            for rr in [1.5, 2.0, 2.5]:
                for spread in [0.75, 1.00]:
                    # 30/70 (canonical), 35/65 (looser — main lever for n).
                    for ob, os_ in [(70, 30), (65, 35)]:
                        for pw in [2, 3]:
                            for plb in [30, 45, 60]:
                                for sep in [3, 5, 7]:
                                    for stop_buf in [0.10, 0.20]:
                                        grid.append(RsiDivergenceParameters(
                                            rsi_period=rsi,
                                            atr_period=atr,
                                            risk_reward=rr,
                                            max_spread=spread,
                                            overbought=float(ob),
                                            oversold=float(os_),
                                            pivot_window=pw,
                                            pivot_lookback=plb,
                                            min_pivot_separation=sep,
                                            stop_buffer_atr=stop_buf,
                                        ))
    return grid


# ── NY Close Compression ────────────────────────────────────────────────────

@dataclass(frozen=True)
class NYCloseCompressionParameters:
    atr_period: int
    risk_reward: float
    max_spread: float
    min_breakout_atr: float
    min_range_atr: float
    max_range_atr: float
    min_range_bars: int


def default_ny_close_compression_grid() -> list[NYCloseCompressionParameters]:
    grid: list[NYCloseCompressionParameters] = []
    for atr in [10, 14, 20]:
        for rr in [1.5, 2.0, 2.5]:
            for spread in [0.75, 1.00, 1.25]:
                for min_ext in [0.03, 0.05, 0.10]:
                    for min_rng in [0.10, 0.15, 0.25]:
                        for max_rng in [0.30, 0.50, 1.0]:
                            for min_bars in [2, 3]:
                                if min_rng >= max_rng:
                                    continue  # skip degenerate combos
                                grid.append(NYCloseCompressionParameters(
                                    atr_period=atr,
                                    risk_reward=rr,
                                    max_spread=spread,
                                    min_breakout_atr=min_ext,
                                    min_range_atr=min_rng,
                                    max_range_atr=max_rng,
                                    min_range_bars=min_bars,
                                ))
    return grid


@dataclass
class _ResearchTask:
    family: str
    timeframe_minutes: int
    bars: tuple[MarketBar, ...]
    parameters: object
    config: BacktestConfig
    train_bars: int
    test_bars: int
    step_bars: int | None
    min_trades: int


def _build_tasks(
    datasets: dict[int, Sequence[MarketBar]],
    families: Sequence[str],
    liquidity_grid: Sequence[LiquiditySweepParameters],
    compression_grid: Sequence[CompressionBreakoutParameters],
    asian_range_grid: Sequence[AsianRangeBreakoutParameters],
    london_breakout_grid: Sequence[LondonBreakoutParameters],
    trend_pullback_grid: Sequence[TrendPullbackParameters],
    ny_session_breakout_grid: Sequence[NYSessionBreakoutParameters],
    momentum_burst_grid: Sequence[MomentumBurstParameters],
    config: BacktestConfig,
    train_bars: int,
    test_bars: int,
    step_bars: int | None,
    min_trades: int,
    session_continuation_grid: Sequence[SessionContinuationParameters] = (),
) -> list[_ResearchTask]:
    tasks: list[_ResearchTask] = []
    enabled_families = {family.strip().lower() for family in families if family.strip()}

    for timeframe_minutes, bars in datasets.items():
        bar_tuple = tuple(bars)
        if "liquidity_sweep" in enabled_families:
            for parameters in liquidity_grid:
                tasks.append(
                    _ResearchTask(
                        family="liquidity_sweep",
                        timeframe_minutes=timeframe_minutes,
                        bars=bar_tuple,
                        parameters=parameters,
                        config=config,
                        train_bars=train_bars,
                        test_bars=test_bars,
                        step_bars=step_bars,
                        min_trades=min_trades,
                    )
                )
        if "compression_breakout" in enabled_families:
            for parameters in compression_grid:
                tasks.append(
                    _ResearchTask(
                        family="compression_breakout",
                        timeframe_minutes=timeframe_minutes,
                        bars=bar_tuple,
                        parameters=parameters,
                        config=config,
                        train_bars=train_bars,
                        test_bars=test_bars,
                        step_bars=step_bars,
                        min_trades=min_trades,
                    )
                )
        if "asian_range_breakout" in enabled_families:
            for parameters in asian_range_grid:
                tasks.append(
                    _ResearchTask(
                        family="asian_range_breakout",
                        timeframe_minutes=timeframe_minutes,
                        bars=bar_tuple,
                        parameters=parameters,
                        config=config,
                        train_bars=train_bars,
                        test_bars=test_bars,
                        step_bars=step_bars,
                        min_trades=min_trades,
                    )
                )
        if "london_breakout" in enabled_families:
            for parameters in london_breakout_grid:
                tasks.append(
                    _ResearchTask(
                        family="london_breakout",
                        timeframe_minutes=timeframe_minutes,
                        bars=bar_tuple,
                        parameters=parameters,
                        config=config,
                        train_bars=train_bars,
                        test_bars=test_bars,
                        step_bars=step_bars,
                        min_trades=min_trades,
                    )
                )
        if "trend_pullback" in enabled_families:
            for parameters in trend_pullback_grid:
                tasks.append(
                    _ResearchTask(
                        family="trend_pullback",
                        timeframe_minutes=timeframe_minutes,
                        bars=bar_tuple,
                        parameters=parameters,
                        config=config,
                        train_bars=train_bars,
                        test_bars=test_bars,
                        step_bars=step_bars,
                        min_trades=min_trades,
                    )
                )
        if "ny_session_breakout" in enabled_families:
            for parameters in ny_session_breakout_grid:
                tasks.append(
                    _ResearchTask(
                        family="ny_session_breakout",
                        timeframe_minutes=timeframe_minutes,
                        bars=bar_tuple,
                        parameters=parameters,
                        config=config,
                        train_bars=train_bars,
                        test_bars=test_bars,
                        step_bars=step_bars,
                        min_trades=min_trades,
                    )
                )
        if "momentum_burst" in enabled_families:
            for parameters in momentum_burst_grid:
                tasks.append(
                    _ResearchTask(
                        family="momentum_burst",
                        timeframe_minutes=timeframe_minutes,
                        bars=bar_tuple,
                        parameters=parameters,
                        config=config,
                        train_bars=train_bars,
                        test_bars=test_bars,
                        step_bars=step_bars,
                        min_trades=min_trades,
                    )
                )
        if "session_continuation" in enabled_families:
            for parameters in session_continuation_grid:
                tasks.append(
                    _ResearchTask(
                        family="session_continuation",
                        timeframe_minutes=timeframe_minutes,
                        bars=bar_tuple,
                        parameters=parameters,
                        config=config,
                        train_bars=train_bars,
                        test_bars=test_bars,
                        step_bars=step_bars,
                        min_trades=min_trades,
                    )
                )

    return tasks


def _evaluate_task(task: _ResearchTask) -> ResearchResult | None:
    strategy = _build_strategy(task.family, task.parameters)
    backtest = run_backtest(task.bars, strategy, task.config)
    summary = summarize_backtest(backtest)
    if summary.total_trades < task.min_trades:
        return None

    walk_forward_results = run_walk_forward(
        bars=task.bars,
        strategy_factory=lambda: _build_strategy(task.family, task.parameters),
        config=task.config,
        train_size=task.train_bars,
        test_size=task.test_bars,
        step_size=task.step_bars,
    )
    walk_forward = summarize_walk_forward(walk_forward_results)

    return ResearchResult(
        family=task.family,
        timeframe_minutes=task.timeframe_minutes,
        parameter_text=_parameter_text(task.parameters),
        summary=summary,
        walk_forward=walk_forward,
    )


def _build_strategy(family: str, parameters: object):
    if family == "liquidity_sweep":
        assert isinstance(parameters, LiquiditySweepParameters)
        return LiquiditySweepStrategy(
            lookback=parameters.lookback,
            atr_period=parameters.atr_period,
            min_sweep_atr=parameters.min_sweep_atr,
            risk_reward=parameters.risk_reward,
            max_spread=parameters.max_spread,
            min_news_distance_minutes=parameters.min_news_distance_minutes,
        )

    if family == "compression_breakout":
        assert isinstance(parameters, CompressionBreakoutParameters)
        return CompressionBreakoutStrategy(
            breakout_lookback=parameters.breakout_lookback,
            compression_lookback=parameters.compression_lookback,
            atr_period=parameters.atr_period,
            max_compression_atr_ratio=parameters.max_compression_atr_ratio,
            min_breakout_atr=parameters.min_breakout_atr,
            risk_reward=parameters.risk_reward,
            max_spread=parameters.max_spread,
            min_news_distance_minutes=parameters.min_news_distance_minutes,
            min_atr_threshold=parameters.min_atr_threshold,
        )

    if family == "asian_range_breakout":
        assert isinstance(parameters, AsianRangeBreakoutParameters)
        return AsianRangeBreakoutStrategy(
            atr_period=parameters.atr_period,
            risk_reward=parameters.risk_reward,
            max_spread=parameters.max_spread,
            min_breakout_atr=parameters.min_breakout_atr,
            min_range_atr=parameters.min_range_atr,
            min_asian_bars=parameters.min_asian_bars,
            min_atr_threshold=parameters.min_atr_threshold,
            min_risk_atr=parameters.min_risk_atr,
        )

    if family == "london_breakout":
        assert isinstance(parameters, LondonBreakoutParameters)
        return LondonBreakoutStrategy(
            opening_range_bars=parameters.opening_range_bars,
            atr_period=parameters.atr_period,
            min_breakout_atr=parameters.min_breakout_atr,
            risk_reward=parameters.risk_reward,
            max_spread=parameters.max_spread,
            min_atr_threshold=parameters.min_atr_threshold,
        )

    if family == "trend_pullback":
        assert isinstance(parameters, TrendPullbackParameters)
        return TrendPullbackStrategy(
            ema_fast=parameters.ema_fast,
            ema_slow=parameters.ema_slow,
            atr_period=parameters.atr_period,
            trend_strength_min=parameters.trend_strength_min,
            pullback_tolerance=parameters.pullback_tolerance,
            risk_reward=parameters.risk_reward,
            max_spread=parameters.max_spread,
        )

    if family == "ny_session_breakout":
        assert isinstance(parameters, NYSessionBreakoutParameters)
        return NYSessionBreakoutStrategy(
            atr_period=parameters.atr_period,
            risk_reward=parameters.risk_reward,
            max_spread=parameters.max_spread,
            min_breakout_atr=parameters.min_breakout_atr,
            min_range_atr=parameters.min_range_atr,
            min_london_bars=parameters.min_london_bars,
        )

    if family == "momentum_burst":
        assert isinstance(parameters, MomentumBurstParameters)
        return MomentumBurstStrategy(
            atr_period=parameters.atr_period,
            min_body_atr=parameters.min_body_atr,
            body_fraction=parameters.body_fraction,
            risk_reward=parameters.risk_reward,
            max_spread=parameters.max_spread,
            min_atr_threshold=parameters.min_atr_threshold,
        )

    if family == "session_continuation":
        assert isinstance(parameters, SessionContinuationParameters)
        from ..strategies.session_continuation import SessionContinuationStrategy
        return SessionContinuationStrategy(
            atr_period=parameters.atr_period,
            risk_reward=parameters.risk_reward,
            max_spread=parameters.max_spread,
            min_session_quantile=parameters.min_session_quantile,
            min_range_atr=parameters.min_range_atr,
            entry_slippage_buffer=parameters.entry_slippage_buffer,
        )

    raise ValueError(f"unsupported strategy family: {family}")


def _parameter_text(parameters: object) -> str:
    parts = [f"{field.name}={getattr(parameters, field.name)}" for field in fields(parameters)]
    return " ".join(parts)


def _resolve_workers(max_workers: int, task_count: int) -> int:
    from ..infra.resource import resolve_workers
    return resolve_workers(max_workers, task_count)


def _sort_key(result: ResearchResult) -> tuple[float, float, float, float, int, float]:
    summary_profit_factor = (
        result.summary.profit_factor if result.summary.profit_factor != float("inf") else 999.0
    )
    return (
        result.walk_forward.positive_window_ratio,
        result.walk_forward.average_r,
        result.summary.average_r,
        summary_profit_factor,
        result.summary.total_trades,
        1.0 - result.summary.max_drawdown,
    )