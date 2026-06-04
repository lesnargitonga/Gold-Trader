"""Pattern miner with FDR control.

Given a feature matrix (boolean per-bar features) and a bar series,
exhaustively evaluate the forward-N-bar return distribution conditional
on every 1-, 2-, and 3-feature conjunction.

Pipeline
--------
1.  ``mine_patterns(bars, fm, ...)`` runs over the **train** slice
    (first ``train_fraction`` of the bars).
2.  For every conjunction, compute:

        n_signals
        mean_r              forward (close[i+H]-close[i])/atr[i]
        win_rate
        t_stat              mean / (sd / sqrt(n))   - simple t
        bootstrap_p         block-bootstrap two-sided p-value, B blocks
                            of size ``block_size`` (handles serial corr)

3.  Filter to ``n_signals >= min_signals`` and ``|mean_r| >= min_effect``.
4.  Apply Benjamini-Hochberg FDR at level ``fdr_q`` across surviving
    candidates, using the bootstrap p-values.
5.  Re-evaluate FDR survivors on the **holdout** slice (last 1-train).
6.  Return ``MinedPattern`` records sorted by holdout effect size.

Multiple-testing corrections are applied to the *bootstrap* p-values not
to the raw t-stats, so the controls remain valid even with autocorrelation
in the bar returns.

Pure stdlib (no numpy / scipy).  Designed to run single-process or via
the global resource governor.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable, Sequence

from ..models import MarketBar
from .features import FeatureMatrix, _atr


@dataclass(frozen=True)
class MinedPattern:
    features: tuple[str, ...]
    n_train: int
    train_mean_r: float
    train_win_rate: float
    train_t_stat: float
    train_p: float
    train_p_adj: float
    n_holdout: int
    holdout_mean_r: float
    holdout_win_rate: float
    holdout_t_stat: float
    holdout_p: float
    direction: str  # "long" | "short" — sign of train mean
    holdout_thirds_consistent: int = 0  # 0..3 — sign-stability across holdout thirds
    holdout_sharpe: float = 0.0          # holdout mean / sd (per-trade Sharpe)


@dataclass(frozen=True)
class MinerConfig:
    horizon_bars: int = 8        # forward return window
    train_fraction: float = 2 / 3
    max_combo_size: int = 2      # 1- and 2-feature conjunctions
    min_signals: int = 30        # train-side floor
    min_effect_r: float = 0.10   # |mean_r| in ATR units
    fdr_q: float = 0.10
    bootstrap_blocks: int = 1000
    block_size: int = 16         # ~ 4 hours of 15m bars
    seed: int = 1729
    holdout_min_signals: int = 10


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _mean_std(xs: Sequence[float]) -> tuple[float, float]:
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    m = sum(xs) / n
    if n == 1:
        return m, 0.0
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(var)


def _t_stat(xs: Sequence[float]) -> float:
    m, sd = _mean_std(xs)
    if sd == 0.0 or len(xs) < 2:
        return 0.0
    return m / (sd / math.sqrt(len(xs)))


def _block_bootstrap_p(
    xs: Sequence[float],
    *,
    blocks: int,
    block_size: int,
    rng: random.Random,
) -> float:
    """Two-sided block-bootstrap p-value for H0: mean(xs) = 0.

    Uses the moving-block bootstrap: resample blocks of contiguous
    observations of length ``block_size`` (with replacement), with
    centring around the sample mean to construct the null distribution.
    Returns the share of bootstrap means whose absolute value exceeds
    the observed |mean|.
    """
    n = len(xs)
    if n < block_size or n < 2:
        return 1.0
    obs_mean, _ = _mean_std(xs)
    centred = [x - obs_mean for x in xs]
    n_blocks = max(1, n // block_size)
    extreme = 0
    for _ in range(blocks):
        s = 0.0
        cnt = 0
        for _b in range(n_blocks):
            start = rng.randrange(0, n - block_size + 1)
            for j in range(block_size):
                s += centred[start + j]
                cnt += 1
        m = s / cnt if cnt else 0.0
        if abs(m) >= abs(obs_mean):
            extreme += 1
    return (extreme + 1) / (blocks + 1)


def _benjamini_hochberg(pvals: Sequence[float], q: float) -> list[bool]:
    """Return per-test boolean reject mask under BH-FDR at level q."""
    n = len(pvals)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: pvals[i])
    thresh = [(k + 1) / n * q for k in range(n)]
    reject_up_to = -1
    for k, idx in enumerate(order):
        if pvals[idx] <= thresh[k]:
            reject_up_to = k
    out = [False] * n
    for k in range(reject_up_to + 1):
        out[order[k]] = True
    return out


def _bh_adjust(pvals: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values."""
    n = len(pvals)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: pvals[i])
    adj = [0.0] * n
    prev = 1.0
    # Walk from largest p down to smallest; adjusted p = min over suffix
    # of n / rank * p_(rank).
    for k in range(n - 1, -1, -1):
        idx = order[k]
        rank = k + 1
        val = min(prev, pvals[idx] * n / rank)
        val = min(1.0, val)
        adj[idx] = val
        prev = val
    return adj


# ---------------------------------------------------------------------------
# Forward returns
# ---------------------------------------------------------------------------


def _forward_returns(
    bars: Sequence[MarketBar],
    horizon: int,
    atr_period: int = 14,
) -> list[float | None]:
    """fwd_r[i] = (close[i+horizon] - close[i]) / atr[i] (or None)."""
    n = len(bars)
    out: list[float | None] = [None] * n
    atr = _atr(bars, atr_period)
    for i in range(n - horizon):
        a = atr[i]
        if a is None or a <= 0:
            continue
        out[i] = (bars[i + horizon].close - bars[i].close) / a
    return out


# ---------------------------------------------------------------------------
# Core miner
# ---------------------------------------------------------------------------


def _eligible_indices(
    fm: FeatureMatrix,
    feats: tuple[str, ...],
    fwd: Sequence[float | None],
    lo: int,
    hi: int,
) -> list[int]:
    """Indices in [lo, hi) where every feature in ``feats`` is True
    AND the forward return is defined."""
    vecs = [fm.vector(f) for f in feats]
    out: list[int] = []
    for i in range(lo, hi):
        if fwd[i] is None:
            continue
        ok = True
        for v in vecs:
            if not v[i]:
                ok = False
                break
        if ok:
            out.append(i)
    return out


def _evaluate(
    indices: Sequence[int],
    fwd: Sequence[float | None],
    *,
    bootstrap_blocks: int,
    block_size: int,
    rng: random.Random,
) -> tuple[int, float, float, float, float]:
    """Return (n, mean_r, win_rate, t_stat, bootstrap_p)."""
    if not indices:
        return 0, 0.0, 0.0, 0.0, 1.0
    rs = [fwd[i] for i in indices]
    rs = [r for r in rs if r is not None]
    if not rs:
        return 0, 0.0, 0.0, 0.0, 1.0
    n = len(rs)
    m, sd = _mean_std(rs)
    win_rate = sum(1 for r in rs if r > 0) / n
    t = m / (sd / math.sqrt(n)) if sd > 0 and n > 1 else 0.0
    p = _block_bootstrap_p(
        rs, blocks=bootstrap_blocks, block_size=block_size, rng=rng,
    )
    return n, m, win_rate, t, p


def mine_patterns(
    bars: Sequence[MarketBar],
    fm: FeatureMatrix,
    *,
    config: MinerConfig | None = None,
    feature_subset: Iterable[str] | None = None,
) -> list[MinedPattern]:
    """Mine for forward-return effects, FDR-controlled, train→holdout split.

    Args:
      bars: full bar series.
      fm: pre-computed FeatureMatrix over the same bars.
      config: see :class:`MinerConfig`.
      feature_subset: optional restriction to a subset of feature names
        (useful for ablation; default = every feature in ``fm``).
    """
    cfg = config or MinerConfig()
    rng = random.Random(cfg.seed)
    n = len(bars)
    if n != fm.bar_count:
        raise ValueError("bars and FeatureMatrix length mismatch")

    fwd = _forward_returns(bars, cfg.horizon_bars)
    train_end = int(n * cfg.train_fraction)
    holdout_lo = train_end
    holdout_hi = n

    feature_pool = list(feature_subset) if feature_subset else fm.names()

    # Build candidate combos.
    combos: list[tuple[str, ...]] = []
    for k in range(1, cfg.max_combo_size + 1):
        for c in combinations(feature_pool, k):
            combos.append(c)

    # First pass — mine on train.
    survivors_raw: list[tuple[
        tuple[str, ...], int, float, float, float, float
    ]] = []  # (feats, n, mean, win, t, p)

    for combo in combos:
        idx = _eligible_indices(fm, combo, fwd, 0, train_end)
        if len(idx) < cfg.min_signals:
            continue
        n_t, mean_r, win, t, p = _evaluate(
            idx, fwd,
            bootstrap_blocks=cfg.bootstrap_blocks,
            block_size=cfg.block_size,
            rng=rng,
        )
        if abs(mean_r) < cfg.min_effect_r:
            continue
        survivors_raw.append((combo, n_t, mean_r, win, t, p))

    if not survivors_raw:
        return []

    # FDR control.
    pvals = [row[5] for row in survivors_raw]
    reject = _benjamini_hochberg(pvals, cfg.fdr_q)
    p_adj = _bh_adjust(pvals)

    # Holdout re-evaluation for survivors only.
    out: list[MinedPattern] = []
    for keep, padj, row in zip(reject, p_adj, survivors_raw):
        if not keep:
            continue
        combo, n_t, mean_r, win, t, p = row
        h_idx = _eligible_indices(fm, combo, fwd, holdout_lo, holdout_hi)
        n_h, h_mean, h_win, h_t, h_p = _evaluate(
            h_idx, fwd,
            bootstrap_blocks=cfg.bootstrap_blocks,
            block_size=cfg.block_size,
            rng=rng,
        )
        if n_h < cfg.holdout_min_signals:
            continue
        direction = "long" if mean_r > 0 else "short"
        # Holdout thirds stability: split holdout into 3 equal slices,
        # count how many show the same sign as the train mean.
        third = (holdout_hi - holdout_lo) // 3
        consistent = 0
        for k in range(3):
            slo = holdout_lo + k * third
            shi = holdout_lo + (k + 1) * third if k < 2 else holdout_hi
            sub = _eligible_indices(fm, combo, fwd, slo, shi)
            if len(sub) < 3:
                continue
            srs = [fwd[i] for i in sub if fwd[i] is not None]
            if not srs:
                continue
            sm = sum(srs) / len(srs)
            if (sm > 0) == (mean_r > 0):
                consistent += 1
        # Per-trade Sharpe on holdout.
        h_rs = [fwd[i] for i in h_idx if fwd[i] is not None]
        _, h_sd = _mean_std(h_rs)
        h_sharpe = h_mean / h_sd if h_sd > 0 else 0.0
        out.append(MinedPattern(
            features=combo,
            n_train=n_t,
            train_mean_r=mean_r,
            train_win_rate=win,
            train_t_stat=t,
            train_p=p,
            train_p_adj=padj,
            n_holdout=n_h,
            holdout_mean_r=h_mean,
            holdout_win_rate=h_win,
            holdout_t_stat=h_t,
            holdout_p=h_p,
            direction=direction,
            holdout_thirds_consistent=consistent,
            holdout_sharpe=h_sharpe,
        ))

    # Sort by holdout absolute mean R.
    out.sort(key=lambda m: -abs(m.holdout_mean_r))
    return out


# ---------------------------------------------------------------------------
# Parallel miner
# ---------------------------------------------------------------------------


_WORKER_BARS: list[MarketBar] | None = None
_WORKER_FM: FeatureMatrix | None = None
_WORKER_FWD: list[float | None] | None = None


def _worker_init(bars, fm, fwd):  # noqa: ANN001
    global _WORKER_BARS, _WORKER_FM, _WORKER_FWD
    _WORKER_BARS = bars
    _WORKER_FM = fm
    _WORKER_FWD = fwd


def _worker_evaluate(
    args: tuple[
        list[tuple[str, ...]], int, int, int, int, float, int, int, int,
    ],
) -> list[tuple[tuple[str, ...], int, float, float, float, float]]:
    """Evaluate a chunk of combos on the train slice."""
    (
        combos, lo, hi, min_signals, bootstrap_blocks, min_effect_r,
        block_size, seed_offset, _,
    ) = args
    fm = _WORKER_FM
    fwd = _WORKER_FWD
    assert fm is not None and fwd is not None
    rng = random.Random(1729 + seed_offset)
    out: list[tuple[tuple[str, ...], int, float, float, float, float]] = []
    for combo in combos:
        idx = _eligible_indices(fm, combo, fwd, lo, hi)
        if len(idx) < min_signals:
            continue
        n_t, mean_r, win, t, p = _evaluate(
            idx, fwd,
            bootstrap_blocks=bootstrap_blocks,
            block_size=block_size,
            rng=rng,
        )
        if abs(mean_r) < min_effect_r:
            continue
        out.append((combo, n_t, mean_r, win, t, p))
    return out


def mine_patterns_parallel(
    bars: Sequence[MarketBar],
    fm: FeatureMatrix,
    *,
    config: MinerConfig | None = None,
    feature_subset: Iterable[str] | None = None,
    n_workers: int | None = None,
    progress: bool = False,
) -> list[MinedPattern]:
    """Parallel version of :func:`mine_patterns`.

    The train pass (the expensive bootstrap loop) is split across worker
    processes via the resource governor.  FDR + holdout re-evaluation
    happens in the main process (cheap, sequential).
    """
    from concurrent.futures import ProcessPoolExecutor
    from ..infra.resource import resolve_workers

    cfg = config or MinerConfig()
    n = len(bars)
    if n != fm.bar_count:
        raise ValueError("bars and FeatureMatrix length mismatch")

    fwd = _forward_returns(bars, cfg.horizon_bars)
    train_end = int(n * cfg.train_fraction)
    holdout_lo, holdout_hi = train_end, n

    feature_pool = list(feature_subset) if feature_subset else fm.names()
    combos: list[tuple[str, ...]] = []
    for k in range(1, cfg.max_combo_size + 1):
        for c in combinations(feature_pool, k):
            combos.append(c)

    if not combos:
        return []

    workers = resolve_workers(n_workers or 0, len(combos))

    survivors_raw: list[tuple[
        tuple[str, ...], int, float, float, float, float
    ]] = []

    if workers <= 1 or len(combos) < 200:
        # Small problem — stay in-process.
        return mine_patterns(
            bars, fm, config=cfg, feature_subset=feature_subset,
        )

    # Split combos into roughly equal chunks.
    chunk_size = max(50, (len(combos) + workers * 4 - 1) // (workers * 4))
    chunks = [
        combos[i:i + chunk_size] for i in range(0, len(combos), chunk_size)
    ]

    tasks = [
        (
            chunk, 0, train_end,
            cfg.min_signals, cfg.bootstrap_blocks, cfg.min_effect_r,
            cfg.block_size, j, 0,
        )
        for j, chunk in enumerate(chunks)
    ]

    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_worker_init,
        initargs=(list(bars), fm, fwd),
    ) as ex:
        done = 0
        for partial in ex.map(_worker_evaluate, tasks):
            survivors_raw.extend(partial)
            done += 1
            if progress:
                print(
                    f"  [{done}/{len(tasks)} chunks done; "
                    f"{len(survivors_raw)} train-side candidates]",
                    flush=True,
                )

    if not survivors_raw:
        return []

    pvals = [row[5] for row in survivors_raw]
    reject = _benjamini_hochberg(pvals, cfg.fdr_q)
    p_adj = _bh_adjust(pvals)

    rng = random.Random(cfg.seed)
    out: list[MinedPattern] = []
    for keep, padj, row in zip(reject, p_adj, survivors_raw):
        if not keep:
            continue
        combo, n_t, mean_r, win, t, p = row
        h_idx = _eligible_indices(fm, combo, fwd, holdout_lo, holdout_hi)
        n_h, h_mean, h_win, h_t, h_p = _evaluate(
            h_idx, fwd,
            bootstrap_blocks=cfg.bootstrap_blocks,
            block_size=cfg.block_size,
            rng=rng,
        )
        if n_h < cfg.holdout_min_signals:
            continue
        # Thirds stability.
        third = (holdout_hi - holdout_lo) // 3
        consistent = 0
        for k in range(3):
            slo = holdout_lo + k * third
            shi = holdout_lo + (k + 1) * third if k < 2 else holdout_hi
            sub = _eligible_indices(fm, combo, fwd, slo, shi)
            if len(sub) < 3:
                continue
            srs = [fwd[i] for i in sub if fwd[i] is not None]
            if not srs:
                continue
            sm = sum(srs) / len(srs)
            if (sm > 0) == (mean_r > 0):
                consistent += 1
        h_rs = [fwd[i] for i in h_idx if fwd[i] is not None]
        _, h_sd = _mean_std(h_rs)
        h_sharpe = h_mean / h_sd if h_sd > 0 else 0.0
        direction = "long" if mean_r > 0 else "short"
        out.append(MinedPattern(
            features=combo, n_train=n_t,
            train_mean_r=mean_r, train_win_rate=win,
            train_t_stat=t, train_p=p, train_p_adj=padj,
            n_holdout=n_h, holdout_mean_r=h_mean,
            holdout_win_rate=h_win, holdout_t_stat=h_t, holdout_p=h_p,
            direction=direction,
            holdout_thirds_consistent=consistent,
            holdout_sharpe=h_sharpe,
        ))

    out.sort(key=lambda m: -abs(m.holdout_mean_r))
    return out
