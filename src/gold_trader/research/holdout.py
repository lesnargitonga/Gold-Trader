from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from ..backtest.engine import run_backtest
from ..backtest.metrics import BacktestSummary, summarize_backtest
from ..models import BacktestConfig, MarketBar
from ..strategies.base import Strategy
from ..validation.walk_forward import (
    WalkForwardAggregate,
    run_true_walk_forward,
    summarize_true_walk_forward,
)
from .parallel_search import parallel_best_params
from .permutation import PermutationTestResult, run_permutation_test


@dataclass(frozen=True)
class HoldoutEvalResult:
    """Full train/holdout evaluation report for one strategy family.

    Attributes
    ----------
    family : str
        Human-readable strategy family name.
    train_bars : int
        Number of bars used for training (parameter selection).
    holdout_bars : int
        Number of bars in the held-out test period.
    best_params : Any
        Parameter object selected by in-sample fitting on the train portion.
    train_pf : float
        Best in-sample profit factor that led to *best_params* selection.
    holdout_summary : BacktestSummary
        Out-of-sample backtest on the held-out slice with *best_params*.
    holdout_permutation : PermutationTestResult
        Permutation test on the held-out trades (answers: is the holdout PF
        statistically distinguishable from random?).
    true_walk_forward : WalkForwardAggregate
        True walk-forward aggregate across the train portion only.
    verdict : str
        Human-readable pass/fail verdict.
    """

    family: str
    train_bars: int
    holdout_bars: int
    best_params: Any
    train_pf: float
    holdout_summary: BacktestSummary
    holdout_permutation: PermutationTestResult
    true_walk_forward: WalkForwardAggregate
    verdict: str


def run_holdout_evaluation(
    bars: Sequence[MarketBar],
    param_grid: Sequence[Any],
    strategy_factory: Callable[[Any], Strategy],
    config: BacktestConfig,
    holdout_fraction: float = 1 / 3,
    wf_train_size: int | None = None,
    wf_test_size: int | None = None,
    min_train_trades: int = 5,
    n_permutations: int = 5_000,
    permutation_seed: int = 42,
    family: str = "strategy",
    family_name: str = "",
    n_workers: int = 1,
    skip_walk_forward: bool = False,
) -> HoldoutEvalResult:
    """Designate a hard hold-out period and evaluate out-of-sample performance.

    Procedure
    ---------
    1. Split ``bars`` into ``train_bars`` (first 1−holdout_fraction) and
       ``holdout_bars`` (last holdout_fraction).  The split is fixed — the
       holdout data is never touched until the final evaluation step.

    2. Run true walk-forward on the train portion to select the best parameter
       set.  Each WF window runs the full ``param_grid`` in-sample and evaluates
       the winner on the test slice.

    3. Select the best ``param_grid`` entry by running a single full-backtest on
       the entire train portion (not just the last WF window) — whichever
       parameter set produces the best in-sample profit factor.

    4. Evaluate the selected parameters on the held-out slice.

    5. Run a sign-randomization permutation test on the held-out trades to
       formally assess statistical significance.

    Parameters
    ----------
    bars : Sequence[MarketBar]
        Full bar history (train + holdout combined).
    param_grid : Sequence[Any]
        Parameter grid to search over.
    strategy_factory : Callable[[Any], Strategy]
        Receives one parameter object, returns a Strategy instance.
    config : BacktestConfig
        Backtest configuration (spread, commission, etc.).
    holdout_fraction : float
        Fraction of bars to reserve as held-out.  Default 1/3 (30-day hold-out
        from a 90-day dataset).
    wf_train_size : int | None
        Walk-forward inner train window size (bars).  Defaults to 60% of the
        train portion.
    wf_test_size : int | None
        Walk-forward inner test window size (bars).  Defaults to 20% of the
        train portion.
    min_train_trades : int
        Minimum trades required in a WF train window to consider a parameter set.
    n_permutations : int
        Number of sign-randomisation iterations for the permutation test.
    permutation_seed : int
        Random seed for permutation test reproducibility.
    family : str
        Label attached to the result for display.
    family_name : str
        Registry key used for parallel param search (e.g. ``"asian_range_breakout"``).
        When non-empty and *n_workers* > 1, the param scan is parallelised.
    n_workers : int
        Number of worker processes for parallel param search.  ``1`` means
        sequential (the default; safe for all environments).
    """
    total = len(bars)
    holdout_start = int(total * (1.0 - holdout_fraction))
    train_slice = bars[:holdout_start]
    holdout_slice = bars[holdout_start:]

    n_train = len(train_slice)
    n_holdout = len(holdout_slice)

    # Use a research config that never halts mid-backtest: kill switch is
    # inappropriate for parameter search since it would unfairly penalise
    # strategies with unlucky early losses and bias param selection.
    research_config = BacktestConfig(
        starting_equity=config.starting_equity,
        risk_fraction=config.risk_fraction,
        max_hold_bars=config.max_hold_bars,
        kill_switch_drawdown_fraction=None,
        commission_per_trade=config.commission_per_trade,
    )

    # ── walk-forward window sizes ──────────────────────────────────────
    wf_train = wf_train_size or int(n_train * 0.60)
    wf_test = wf_test_size or int(n_train * 0.20)
    if wf_train < 1:
        wf_train = 1
    if wf_test < 1:
        wf_test = 1

    # ── true walk-forward on train portion ────────────────────────────
    if skip_walk_forward:
        # Empty aggregate; verdict gate that uses positive_window_ratio is skipped
        # naturally because there are no windows.
        wf_results = []
        wf_aggregate = summarize_true_walk_forward(wf_results)
    else:
        wf_results = run_true_walk_forward(
            bars=train_slice,
            param_grid=param_grid,
            strategy_factory=strategy_factory,
            config=research_config,
            train_size=wf_train,
            test_size=wf_test,
            min_train_trades=min_train_trades,
            n_workers=n_workers if family_name else 1,
            family_name=family_name,
        )
        wf_aggregate = summarize_true_walk_forward(wf_results)

    # ── select best params via full-train backtest ────────────────────
    if family_name and n_workers > 1:
        best_params, best_pf = parallel_best_params(
            family_name=family_name,
            param_grid=param_grid,
            bars=train_slice,
            config=research_config,
            n_workers=n_workers,
            min_train_trades=min_train_trades,
        )
        if best_params is None:
            best_params = param_grid[0]
    else:
        best_params: Any = param_grid[0] if param_grid else None
        best_pf = -1.0
        for params in param_grid:
            s = strategy_factory(params)
            result = run_backtest(train_slice, s, research_config)
            summary = summarize_backtest(result)
            if summary.total_trades < min_train_trades:
                continue
            pf = summary.profit_factor if summary.profit_factor != float("inf") else 999.0
            if pf > best_pf:
                best_pf = pf
                best_params = params

    # ── evaluate selected params on held-out slice ────────────────────
    holdout_strategy = strategy_factory(best_params)
    holdout_result = run_backtest(holdout_slice, holdout_strategy, research_config)
    holdout_summary = summarize_backtest(holdout_result)

    # ── permutation test on held-out trades ───────────────────────────
    holdout_permutation = run_permutation_test(
        bars=holdout_slice,
        strategy=holdout_strategy,
        config=research_config,
        n_permutations=n_permutations,
        seed=permutation_seed,
    )

    # ── verdict ───────────────────────────────────────────────────────
    oos_pf = (
        holdout_summary.profit_factor
        if holdout_summary.profit_factor != float("inf")
        else 999.0
    )
    verdict = _make_verdict(
        oos_pf=oos_pf,
        oos_trades=holdout_summary.total_trades,
        wf_positive_ratio=wf_aggregate.positive_window_ratio,
        p_value=holdout_permutation.p_value,
    )

    return HoldoutEvalResult(
        family=family,
        train_bars=n_train,
        holdout_bars=n_holdout,
        best_params=best_params,
        train_pf=best_pf,
        holdout_summary=holdout_summary,
        holdout_permutation=holdout_permutation,
        true_walk_forward=wf_aggregate,
        verdict=verdict,
    )


def _make_verdict(
    oos_pf: float,
    oos_trades: int,
    wf_positive_ratio: float,
    p_value: float,
) -> str:
    if oos_trades < 5:
        return "INCONCLUSIVE: fewer than 5 held-out trades — dataset too short to evaluate"
    if p_value >= 0.20:
        return (
            f"FAIL: held-out p={p_value:.3f} (NOISE) — "
            f"PF={oos_pf:.2f} indistinguishable from random, strategy must be rebuilt"
        )
    if oos_pf < 1.10:
        return (
            f"FAIL: held-out PF={oos_pf:.2f} < 1.10 threshold "
            f"(p={p_value:.3f}) — marginal edge, not tradeable"
        )
    if wf_positive_ratio > 0 and wf_positive_ratio < 0.50:
        return (
            f"WEAK: held-out PF={oos_pf:.2f} p={p_value:.3f} but WF positive ratio "
            f"{wf_positive_ratio:.0%} < 50% — regime-dependent, unreliable across time"
        )
    return (
        f"PASS: held-out PF={oos_pf:.2f} p={p_value:.3f} "
        f"WF_pos={wf_positive_ratio:.0%} — statistically significant consistent edge"
    )
