from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ..backtest.engine import run_backtest
from ..backtest.metrics import BacktestSummary, summarize_backtest
from ..models import BacktestConfig, MarketBar
from ..strategies.base import Strategy


@dataclass(frozen=True)
class WalkForwardWindow:
    train_start: int
    train_end: int
    test_start: int
    test_end: int


@dataclass(frozen=True)
class WalkForwardResult:
    window: WalkForwardWindow
    summary: BacktestSummary


@dataclass(frozen=True)
class WalkForwardAggregate:
    window_count: int
    positive_window_count: int
    positive_window_ratio: float
    average_r: float
    average_return: float
    average_profit_factor: float
    worst_drawdown: float
    total_test_trades: int


def build_walk_forward_windows(
    total_bars: int,
    train_size: int,
    test_size: int,
    step_size: int | None = None,
) -> list[WalkForwardWindow]:
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size and test_size must be positive")

    step = step_size or test_size
    if step <= 0:
        raise ValueError("step_size must be positive")

    windows: list[WalkForwardWindow] = []
    train_start = 0
    while train_start + train_size + test_size <= total_bars:
        train_end = train_start + train_size
        test_end = train_end + test_size
        windows.append(
            WalkForwardWindow(
                train_start=train_start,
                train_end=train_end,
                test_start=train_end,
                test_end=test_end,
            )
        )
        train_start += step

    return windows


def run_walk_forward(
    bars: Sequence[MarketBar],
    strategy_factory: Callable[[], Strategy],
    config: BacktestConfig,
    train_size: int,
    test_size: int,
    step_size: int | None = None,
) -> list[WalkForwardResult]:
    windows = build_walk_forward_windows(
        total_bars=len(bars),
        train_size=train_size,
        test_size=test_size,
        step_size=step_size,
    )
    results: list[WalkForwardResult] = []

    for window in windows:
        test_slice = bars[window.test_start:window.test_end]
        backtest = run_backtest(test_slice, strategy_factory(), config)
        results.append(
            WalkForwardResult(
                window=window,
                summary=summarize_backtest(backtest),
            )
        )

    return results


def summarize_walk_forward(results: Sequence[WalkForwardResult]) -> WalkForwardAggregate:
    if not results:
        return WalkForwardAggregate(
            window_count=0,
            positive_window_count=0,
            positive_window_ratio=0.0,
            average_r=0.0,
            average_return=0.0,
            average_profit_factor=0.0,
            worst_drawdown=0.0,
            total_test_trades=0,
        )

    positive_window_count = sum(1 for result in results if result.summary.total_return > 0.0)
    finite_profit_factors = [
        result.summary.profit_factor
        for result in results
        if result.summary.profit_factor != float("inf")
    ]
    average_profit_factor = (
        sum(finite_profit_factors) / len(finite_profit_factors) if finite_profit_factors else 999.0
    )

    return WalkForwardAggregate(
        window_count=len(results),
        positive_window_count=positive_window_count,
        positive_window_ratio=positive_window_count / len(results),
        average_r=sum(result.summary.average_r for result in results) / len(results),
        average_return=sum(result.summary.total_return for result in results) / len(results),
        average_profit_factor=average_profit_factor,
        worst_drawdown=max(result.summary.max_drawdown for result in results),
        total_test_trades=sum(result.summary.total_trades for result in results),
    )


@dataclass(frozen=True)
class TrueWalkForwardResult:
    """Result of one window of true walk-forward validation.

    *train_best_pf*   — best in-sample profit factor from the parameter grid.
    *test_summary*    — out-of-sample backtest using those exact parameters.
    *selected_params* — parameter object that won on the training slice.
    """

    window: WalkForwardWindow
    train_best_pf: float
    test_summary: BacktestSummary
    selected_params: Any


def run_true_walk_forward(
    bars: Sequence[MarketBar],
    param_grid: Sequence[Any],
    strategy_factory: Callable[[Any], Strategy],
    config: BacktestConfig,
    train_size: int,
    test_size: int,
    step_size: int | None = None,
    min_train_trades: int = 5,
    n_workers: int = 1,
    family_name: str = "",
) -> list[TrueWalkForwardResult]:
    """True walk-forward validation.

    For each window:
    1. Grid-search *param_grid* on the training slice; pick the param set with the
       highest in-sample profit factor (requiring at least *min_train_trades*).
    2. Evaluate the selected params on the test slice.
    3. Record test-slice performance only.

    This is the correct validation procedure: parameter selection happens in-sample
    and is never informed by the test slice.
    """
    windows = build_walk_forward_windows(
        total_bars=len(bars),
        train_size=train_size,
        test_size=test_size,
        step_size=step_size,
    )
    results: list[TrueWalkForwardResult] = []

    for window in windows:
        train_bars = list(bars[window.train_start:window.train_end])
        test_bars = bars[window.test_start:window.test_end]

        if family_name and n_workers > 1:
            # parallel inner-grid scan for this WF window
            from ..research.parallel_search import parallel_best_params
            best_params, best_pf = parallel_best_params(
                family_name=family_name,
                param_grid=param_grid,
                bars=train_bars,
                config=config,
                n_workers=n_workers,
                min_train_trades=min_train_trades,
            )
        else:
            best_params: Any = None
            best_pf = -1.0
            for params in param_grid:
                strategy = strategy_factory(params)
                train_result = run_backtest(train_bars, strategy, config)
                train_summary = summarize_backtest(train_result)
                if train_summary.total_trades < min_train_trades:
                    continue
                pf = (
                    train_summary.profit_factor
                    if train_summary.profit_factor != float("inf")
                    else 999.0
                )
                if pf > best_pf:
                    best_pf = pf
                    best_params = params

        if best_params is None:
            continue

        test_result = run_backtest(test_bars, strategy_factory(best_params), config)
        results.append(
            TrueWalkForwardResult(
                window=window,
                train_best_pf=best_pf,
                test_summary=summarize_backtest(test_result),
                selected_params=best_params,
            )
        )

    return results


def summarize_true_walk_forward(results: Sequence[TrueWalkForwardResult]) -> WalkForwardAggregate:
    """Summarise true walk-forward results using the same aggregate schema."""
    if not results:
        return WalkForwardAggregate(
            window_count=0,
            positive_window_count=0,
            positive_window_ratio=0.0,
            average_r=0.0,
            average_return=0.0,
            average_profit_factor=0.0,
            worst_drawdown=0.0,
            total_test_trades=0,
        )

    positive_window_count = sum(
        1 for result in results if result.test_summary.total_return > 0.0
    )
    finite_pfs = [
        result.test_summary.profit_factor
        for result in results
        if result.test_summary.profit_factor != float("inf")
    ]
    average_pf = sum(finite_pfs) / len(finite_pfs) if finite_pfs else 999.0

    return WalkForwardAggregate(
        window_count=len(results),
        positive_window_count=positive_window_count,
        positive_window_ratio=positive_window_count / len(results),
        average_r=sum(r.test_summary.average_r for r in results) / len(results),
        average_return=sum(r.test_summary.total_return for r in results) / len(results),
        average_profit_factor=average_pf,
        worst_drawdown=max(r.test_summary.max_drawdown for r in results),
        total_test_trades=sum(r.test_summary.total_trades for r in results),
    )