from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from itertools import product
from typing import Iterable, Sequence

from ..backtest import BacktestSummary, summarize_backtest
from ..backtest.engine import run_backtest
from ..models import BacktestConfig, MarketBar
from ..strategies import LiquiditySweepStrategy


@dataclass(frozen=True)
class LiquiditySweepParameters:
    lookback: int
    atr_period: int
    min_sweep_atr: float
    risk_reward: float
    max_spread: float
    min_news_distance_minutes: float


@dataclass(frozen=True)
class SweepResult:
    parameters: LiquiditySweepParameters
    summary: BacktestSummary


def build_liquidity_sweep_grid(
    lookbacks: Iterable[int],
    atr_periods: Iterable[int],
    min_sweep_atrs: Iterable[float],
    risk_rewards: Iterable[float],
    max_spreads: Iterable[float],
    min_news_distances: Iterable[float],
) -> list[LiquiditySweepParameters]:
    grid = [
        LiquiditySweepParameters(
            lookback=lookback,
            atr_period=atr_period,
            min_sweep_atr=min_sweep_atr,
            risk_reward=risk_reward,
            max_spread=max_spread,
            min_news_distance_minutes=min_news_distance,
        )
        for lookback, atr_period, min_sweep_atr, risk_reward, max_spread, min_news_distance in product(
            tuple(lookbacks),
            tuple(atr_periods),
            tuple(min_sweep_atrs),
            tuple(risk_rewards),
            tuple(max_spreads),
            tuple(min_news_distances),
        )
    ]
    if not grid:
        raise ValueError("parameter grid is empty")
    return grid


def run_liquidity_sweep_sweep(
    bars: Sequence[MarketBar],
    config: BacktestConfig,
    parameter_grid: Sequence[LiquiditySweepParameters],
    max_workers: int = 1,
) -> list[SweepResult]:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    from ..infra.resource import resolve_workers
    max_workers = resolve_workers(max_workers, len(parameter_grid))

    if max_workers == 1:
        results = [_evaluate_parameter_set(bars, config, params) for params in parameter_grid]
    else:
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            results = list(
                executor.map(
                    _evaluate_parameter_set_parallel,
                    ((bars, config, params) for params in parameter_grid),
                )
            )

    return sorted(results, key=_sort_key, reverse=True)


def _evaluate_parameter_set(
    bars: Sequence[MarketBar],
    config: BacktestConfig,
    params: LiquiditySweepParameters,
) -> SweepResult:
    strategy = LiquiditySweepStrategy(
        lookback=params.lookback,
        atr_period=params.atr_period,
        min_sweep_atr=params.min_sweep_atr,
        risk_reward=params.risk_reward,
        max_spread=params.max_spread,
        min_news_distance_minutes=params.min_news_distance_minutes,
    )
    backtest = run_backtest(bars, strategy, config)
    return SweepResult(parameters=params, summary=summarize_backtest(backtest))


def _evaluate_parameter_set_parallel(
    payload: tuple[Sequence[MarketBar], BacktestConfig, LiquiditySweepParameters],
) -> SweepResult:
    bars, config, params = payload
    return _evaluate_parameter_set(bars, config, params)


def _sort_key(result: SweepResult) -> tuple[float, float, int, float]:
    summary = result.summary
    profit_factor = summary.profit_factor if summary.profit_factor != float("inf") else 999.0
    return (
        summary.average_r,
        profit_factor,
        summary.total_trades,
        -summary.max_drawdown,
    )