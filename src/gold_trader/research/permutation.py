from __future__ import annotations

import random
from dataclasses import dataclass
from statistics import mean, median
from typing import Sequence

from ..backtest.engine import run_backtest
from ..backtest.metrics import summarize_backtest
from ..models import BacktestConfig, MarketBar
from ..strategies.base import Strategy


@dataclass(frozen=True)
class PermutationTestResult:
    observed_pf: float
    observed_avg_r: float
    n_trades: int
    n_permutations: int
    p_value: float
    percentile_rank: float
    null_mean_pf: float
    null_median_pf: float
    verdict: str


def run_permutation_test(
    bars: Sequence[MarketBar],
    strategy: Strategy,
    config: BacktestConfig,
    n_permutations: int = 10_000,
    seed: int = 42,
) -> PermutationTestResult:
    """Sign-randomization permutation test.

    Under the null hypothesis that entry direction has no predictive value, each
    trade's outcome is equally likely to be a win or a loss of the same magnitude.
    We randomly flip the sign of each trade PnL *n_permutations* times and check
    what proportion of those null-distribution profit factors exceed the observed PF.

    p-value: fraction of permutations with PF >= observed PF.
    A low p-value (< 0.05) indicates statistically significant edge.
    """
    result = run_backtest(bars, strategy, config)
    trades = list(result.trades)
    if not trades:
        return PermutationTestResult(
            observed_pf=0.0,
            observed_avg_r=0.0,
            n_trades=0,
            n_permutations=n_permutations,
            p_value=1.0,
            percentile_rank=0.0,
            null_mean_pf=0.0,
            null_median_pf=0.0,
            verdict="INCONCLUSIVE: no trades generated",
        )

    summary = summarize_backtest(result)
    observed_pf = summary.profit_factor if summary.profit_factor != float("inf") else 999.0
    magnitudes = [abs(t.pnl) for t in trades]

    rng = random.Random(seed)
    null_pfs: list[float] = []
    for _ in range(n_permutations):
        signs = [rng.choice((1, -1)) for _ in magnitudes]
        pnls = [m * s for m, s in zip(magnitudes, signs)]
        w = sum(p for p in pnls if p > 0)
        l = sum(abs(p) for p in pnls if p < 0)
        null_pfs.append(w / l if l > 0 else 0.0)

    count_gte = sum(1 for pf in null_pfs if pf >= observed_pf)
    p_value = count_gte / n_permutations
    percentile_rank = (1.0 - p_value) * 100.0

    if p_value < 0.05:
        verdict = "SIGNAL: p<0.05 — statistically significant edge at 95% confidence"
    elif p_value < 0.10:
        verdict = "WEAK SIGNAL: p<0.10 — some evidence of edge, needs more data"
    elif p_value < 0.20:
        verdict = "MARGINAL: p<0.20 — inconclusive, results within noise band"
    else:
        verdict = "NOISE: p>=0.20 — no demonstrated edge, strategy should be rebuilt"

    return PermutationTestResult(
        observed_pf=observed_pf,
        observed_avg_r=summary.average_r,
        n_trades=len(trades),
        n_permutations=n_permutations,
        p_value=p_value,
        percentile_rank=percentile_rank,
        null_mean_pf=mean(null_pfs),
        null_median_pf=median(null_pfs),
        verdict=verdict,
    )
