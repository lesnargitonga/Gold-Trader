"""Three-tier filter scoring system for strategy signals.

Background
----------
Binary pass/fail filtering of the canonical discretionary checklist
collapsed IFVG signal counts on 5y/15m from 14,909 to 53 (with holdout
n=7 PF=0.37 NOISE) — textbook overfitting on a tiny sample.  This
module replaces the binary stack with a three-tier architecture:

    Tier 1 — Universal vetos.   Hard kill: news, spread cap,
                                kill-switch, stale tick, weekend
                                proximity.  No score compensates.
    Tier 2 — Strategy vetos.    A handful of structural existence
                                preconditions per family (e.g. for
                                IFVG: a prior liquidity sweep).
                                Without them the signal is meaningless.
    Tier 3 — Scored filters.    Everything else contributes weighted
                                points.  3-way filters give full /
                                partial / zero credit (e.g. HTF
                                aligned 20, neutral 8, opposing 0).

The total scored points are designed to sum to 100 across the enabled
scored filters.  Verdicts (defaults, calibrated empirically):

    score >= 70   FULL_SIZE  (1.0× risk)
    55..69        HALF_SIZE  (0.5× risk)
    40..54        LOG_ONLY   (size 0 — paper-only, calibration data)
    < 40          REJECT     (drop entirely)

Strategies declare a list of ``FilterSpec`` (callable, tier, max_points,
3-way thresholds) and the engine walks them in order: any veto fails
=> immediate REJECT; otherwise the scored sum determines verdict.

This module is pure-stdlib and has no dependency on the strategies
package internals.  The filter callables are wrappers around the
predicates in ``filters.py`` that return ``(passed, points, reason)``
or simply ``(passed, reason)`` for binary scored filters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable


class FilterTier(str, Enum):
    UNIVERSAL_VETO = "universal_veto"
    STRATEGY_VETO = "strategy_veto"
    SCORED = "scored"


class ScoreVerdict(str, Enum):
    FULL_SIZE = "full_size"
    HALF_SIZE = "half_size"
    LOG_ONLY = "log_only"
    REJECT = "reject"


# Default thresholds — calibrate from real holdout score distributions.
DEFAULT_FULL_THRESHOLD = 70
DEFAULT_HALF_THRESHOLD = 55
DEFAULT_LOG_THRESHOLD = 40

DEFAULT_SIZE_MULTIPLIERS: dict[ScoreVerdict, float] = {
    ScoreVerdict.FULL_SIZE: 1.0,
    ScoreVerdict.HALF_SIZE: 0.5,
    ScoreVerdict.LOG_ONLY: 0.0,
    ScoreVerdict.REJECT: 0.0,
}


@dataclass(frozen=True)
class FilterResult:
    name: str
    tier: FilterTier
    passed: bool
    points: float
    max_points: float
    reason: str


@dataclass(frozen=True)
class SignalScore:
    score: float
    max_score: float
    verdict: ScoreVerdict
    size_multiplier: float
    results: tuple[FilterResult, ...] = field(default_factory=tuple)
    vetoed_by: str | None = None  # name of failing veto, if any

    @property
    def trade_allowed(self) -> bool:
        return self.size_multiplier > 0.0

    @property
    def loggable(self) -> bool:
        """Verdict above REJECT — signal worth recording (for calibration)."""
        return self.verdict is not ScoreVerdict.REJECT


def classify_score(
    score: float,
    *,
    full_threshold: int = DEFAULT_FULL_THRESHOLD,
    half_threshold: int = DEFAULT_HALF_THRESHOLD,
    log_threshold: int = DEFAULT_LOG_THRESHOLD,
) -> ScoreVerdict:
    if score >= full_threshold:
        return ScoreVerdict.FULL_SIZE
    if score >= half_threshold:
        return ScoreVerdict.HALF_SIZE
    if score >= log_threshold:
        return ScoreVerdict.LOG_ONLY
    return ScoreVerdict.REJECT


def aggregate_results(
    results: list[FilterResult],
    *,
    full_threshold: int = DEFAULT_FULL_THRESHOLD,
    half_threshold: int = DEFAULT_HALF_THRESHOLD,
    log_threshold: int = DEFAULT_LOG_THRESHOLD,
    size_multipliers: dict[ScoreVerdict, float] | None = None,
) -> SignalScore:
    """Aggregate a sequence of FilterResults into a SignalScore.

    Vetos (UNIVERSAL_VETO + STRATEGY_VETO) short-circuit: any failing
    veto produces an immediate REJECT regardless of scored points.
    Scored filter points sum into the total (including negative
    penalties from ``scored_penalty``); verdict is derived from
    thresholds.  Negative cumulative scores clamp to 0 for verdict
    purposes (they remain visible as ``score`` for journaling).
    """
    sm = size_multipliers or DEFAULT_SIZE_MULTIPLIERS
    score = 0.0
    max_score = 0.0
    vetoed_by: str | None = None
    for r in results:
        if r.tier in (FilterTier.UNIVERSAL_VETO, FilterTier.STRATEGY_VETO):
            if not r.passed and vetoed_by is None:
                vetoed_by = r.name
        else:
            # Penalty filters contribute negative points but don't
            # raise the maximum achievable score.
            if r.max_points > 0:
                max_score += r.max_points
            score += r.points
    if vetoed_by is not None:
        return SignalScore(
            score=score,
            max_score=max_score,
            verdict=ScoreVerdict.REJECT,
            size_multiplier=0.0,
            results=tuple(results),
            vetoed_by=vetoed_by,
        )
    verdict = classify_score(
        max(score, 0.0),
        full_threshold=full_threshold,
        half_threshold=half_threshold,
        log_threshold=log_threshold,
    )
    return SignalScore(
        score=score,
        max_score=max_score,
        verdict=verdict,
        size_multiplier=sm[verdict],
        results=tuple(results),
        vetoed_by=None,
    )


# ---------------------------------------------------------------------------
# Helpers for wrapping predicate functions into FilterResults
# ---------------------------------------------------------------------------
def veto(
    name: str,
    tier: FilterTier,
    predicate: Callable[[], tuple[bool, str]],
) -> FilterResult:
    """Wrap a binary predicate as a veto FilterResult (0/0 points)."""
    passed, reason = predicate()
    return FilterResult(
        name=name,
        tier=tier,
        passed=passed,
        points=0.0,
        max_points=0.0,
        reason=reason,
    )


def scored_binary(
    name: str,
    points: int,
    predicate: Callable[[], tuple[bool, str]],
) -> FilterResult:
    """Scored binary filter: full points on pass, zero on fail."""
    passed, reason = predicate()
    return FilterResult(
        name=name,
        tier=FilterTier.SCORED,
        passed=passed,
        points=float(points) if passed else 0.0,
        max_points=float(points),
        reason=reason,
    )


def scored_three_way(
    name: str,
    points_full: int,
    points_partial: int,
    classifier: Callable[[], tuple[str, str]],
) -> FilterResult:
    """3-way scored filter.

    ``classifier`` returns ``(bucket, reason)`` where bucket is one of
    ``"full"`` / ``"partial"`` / ``"none"``.  Points awarded accordingly.
    """
    bucket, reason = classifier()
    if bucket == "full":
        pts = float(points_full)
        passed = True
    elif bucket == "partial":
        pts = float(points_partial)
        passed = True
    else:
        pts = 0.0
        passed = False
    return FilterResult(
        name=name,
        tier=FilterTier.SCORED,
        passed=passed,
        points=pts,
        max_points=float(points_full),
        reason=reason,
    )


def scored_penalty(
    name: str,
    penalty_points: int,
    predicate: Callable[[], tuple[bool, str]],
) -> FilterResult:
    """Penalty filter — contributes a negative score when predicate trips.

    ``predicate()`` returns ``(tripped, reason)``.  When ``tripped`` is
    True, the filter contributes ``-penalty_points`` to the cumulative
    score; otherwise it contributes 0.  Penalties never raise
    ``max_score``.  Use for "fake confluence" punishment — e.g. trading
    a counter-trend reversal against the higher-timeframe trend should
    cost you 20 points, not just earn zero credit.

    ``passed`` is True when the predicate did NOT trip (no penalty
    applied) — semantically "this aspect was clean".
    """
    tripped, reason = predicate()
    return FilterResult(
        name=name,
        tier=FilterTier.SCORED,
        passed=not tripped,
        points=-float(penalty_points) if tripped else 0.0,
        max_points=0.0,  # penalties don't raise the ceiling
        reason=reason,
    )
