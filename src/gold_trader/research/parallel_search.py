"""Parallel parameter grid search for holdout evaluation and walk-forward.

Uses ``ProcessPoolExecutor`` with the *initializer* pattern so that the large
bars list is copied into each worker process once at startup (not once per
task).  This is critical for performance: with 729 param combinations and
21 k bars, per-task serialisation would dominate runtime (~16× speedup on 16
cores compared with sequential search).

How it works
------------
1. ``parallel_best_params(...)`` spawns N worker processes via
   ``ProcessPoolExecutor(initializer=_worker_init, ...)``.
2. Each worker runs ``_worker_init(bars, config)`` once at startup, storing
   bars and config in module-level globals inside *that worker process*.
3. The main process sends only ``(family_name, params)`` tuples per task —
   a few dozen bytes rather than several MB of bars.
4. ``_eval_param_worker((family, params))`` reads bars/config from
   process-local globals, runs one backtest, and returns
   ``(params, profit_factor, n_trades)``.
5. The main process collects all results and returns the best params.
"""
from __future__ import annotations

import math
import os
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Sequence

from ..models import BacktestConfig, MarketBar

# ── module-level globals populated by the pool initializer in each worker ──
_WORKER_BARS: list[MarketBar] = []
_WORKER_CONFIG: BacktestConfig | None = None


def _worker_init(bars: list[MarketBar], config: BacktestConfig) -> None:
    """Initialise worker-process globals.  Called once per worker at spawn."""
    global _WORKER_BARS, _WORKER_CONFIG  # noqa: PLW0603
    _WORKER_BARS = bars
    _WORKER_CONFIG = config


def _eval_param_worker(args: tuple[str, Any]) -> tuple[Any, float, int]:
    """Evaluate one *(family_name, params)* combo using worker-local state.

    Returns ``(params, profit_factor, n_trades)``.

    Must be a top-level module function (not a closure) to be picklable by
    the multiprocessing machinery.
    """
    family_name, params = args
    from ..backtest.engine import run_backtest
    from ..backtest.metrics import summarize_backtest
    from ._factory_registry import make_strategy

    strategy = make_strategy(family_name, params)
    result = run_backtest(_WORKER_BARS, strategy, _WORKER_CONFIG)  # type: ignore[arg-type]
    summary = summarize_backtest(result)
    pf = summary.profit_factor if summary.profit_factor != float("inf") else 999.0
    return params, pf, summary.total_trades


def parallel_best_params(
    family_name: str,
    param_grid: Sequence[Any],
    bars: Sequence[MarketBar],
    config: BacktestConfig,
    n_workers: int | None = None,
    min_train_trades: int = 5,
) -> tuple[Any, float]:
    """Search *param_grid* in parallel; return ``(best_params, best_pf)``.

    Parameters
    ----------
    family_name:
        Strategy family identifier forwarded to :func:`make_strategy`.
    param_grid:
        Parameter objects to search over.
    bars:
        Training bars — copied to each worker once via the pool initializer.
    config:
        Backtest config (kill-switch should already be ``None`` for research).
    n_workers:
        Worker count.  ``None`` defers to ``infra.resource.cpu_budget``,
        which honours the ``GOLD_MAX_WORKERS`` env var.  Any positive
        value is still capped by the global budget so a stray
        ``--max-workers 64`` cannot pin the box.
    min_train_trades:
        Minimum trades required to consider a parameter set valid.
    """
    if not param_grid:
        return None, -1.0

    from ..infra.resource import resolve_workers
    n_workers = resolve_workers(n_workers or 0, len(param_grid))
    bars_list = list(bars)

    tasks = [(family_name, p) for p in param_grid]
    chunksize = max(1, len(tasks) // (n_workers * 4))

    best_params: Any = param_grid[0]
    best_pf = -1.0
    best_score = -1.0  # selection criterion: pf × log(n_trades)

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_worker_init,
        initargs=(bars_list, config),
    ) as executor:
        for params, pf, n_trades in executor.map(
            _eval_param_worker, tasks, chunksize=chunksize
        ):
            if n_trades < min_train_trades:
                continue
            # PF × log(trades): penalises high-PF results from few trades.
            # log(5)≈1.61, log(30)≈3.40, log(100)≈4.61 — a param set with
            # 60 trades must clear a 34% higher bar than one with 150 trades.
            score = pf * math.log(n_trades)
            if score > best_score:
                best_score = score
                best_pf = pf
                best_params = params

    return best_params, best_pf
