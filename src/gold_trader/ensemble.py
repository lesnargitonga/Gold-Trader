"""Ensemble layer — strategy-level meta-scoring & live signal-strength.

The observatory (``scripts/strategy_observatory.py``) records every
signal every strategy emits, with its score and outcome.  From the
resulting ``per_strategy.csv`` we derive a per-strategy *weight*
(0..1) that says "how much should we trust this strategy when it
fires", grounded in ``PF × √n``.

At live-signal time, each emitted signal carries:

    inside_score      — universal scorer 0..100 (or strategy-internal)
    strategy_weight   — 0..1 from observatory's ranking
    concurrence_count — how many distinct strategies fire same bar

We combine them into a single 0..100 ``signal_strength``::

    signal_strength = inside_score
                    × strategy_weight
                    × concurrence_multiplier(concurrence_count)

The concurrence multiplier is empirically calibrated (see HANDBOOK
"Strategy observatory" — first-look 5y/15m showed concurrence ≥ 3 →
joint PF=2.41 vs alone PF=0.81).

Weights are loaded from a JSON file at ``data/strategy_weights.json``
written by ``scripts/compute_strategy_weights.py``.  Missing files /
unknown strategies fall back to weight=1.0 (unweighted) so the
ensemble layer is always safe to call.
"""
from __future__ import annotations

import json
from pathlib import Path

# Empirically derived from observatory full-grid run (50 combos × 15
# families on 5y/15m, n=1,151 deduped by strategy).  The original
# n=13-at-concurrence-3 finding collapsed under more data: PF=2.41
# became PF=0.36 (n=54), and PF=∞ at concurrence=4 became PF=0.73 (n=36).
# The genuine alpha threshold is concurrence ≥ 5.  Multipliers below
# track measured PF/baseline ratios with caps for sample-size safety.
#
#   concurrent  n      PF    multiplier rationale
#       1      859   0.76    1.00 baseline
#       2      174   0.66    0.85 slightly worse than alone
#       3       54   0.36    0.50 actively bad — demote
#       4       36   0.73    0.95 back to baseline
#       5       15   1.28    1.50
#       6        6   1.58    1.75
#       7        7   inf     2.00 (cap, n=7 wins=7/7)
DEFAULT_CONCURRENCE_TABLE: dict[int, float] = {
    1: 1.0,
    2: 0.85,
    3: 0.50,
    4: 0.95,
    5: 1.50,
    6: 1.75,
    7: 2.0,
}


def concurrence_multiplier(count: int, table: dict[int, float] | None = None) -> float:
    t = table if table is not None else DEFAULT_CONCURRENCE_TABLE
    if count <= 0:
        return 1.0
    if count in t:
        return t[count]
    # Beyond observed bins: clamp to highest tabulated value
    return t[max(t)]


_WEIGHT_CACHE: dict[str, dict[str, float]] = {}


def load_strategy_weights(
    path: str | Path = "data/strategy_weights.json",
) -> dict[str, float]:
    """Load strategy → weight (0..1) mapping.  Missing file → empty dict."""
    p = Path(path)
    key = str(p.resolve())
    if key in _WEIGHT_CACHE:
        return _WEIGHT_CACHE[key]
    if not p.exists():
        _WEIGHT_CACHE[key] = {}
        return _WEIGHT_CACHE[key]
    with p.open() as f:
        raw = json.load(f)
    weights = {str(k): float(v) for k, v in raw.get("weights", {}).items()}
    _WEIGHT_CACHE[key] = weights
    return weights


def strategy_weight(name: str, weights: dict[str, float] | None = None) -> float:
    """Return weight for ``name`` (0..1).  Default 1.0 if unknown."""
    w = weights if weights is not None else load_strategy_weights()
    return float(w.get(name, 1.0))


def signal_strength(
    inside_score: float,
    strategy_name: str,
    *,
    concurrence: int = 1,
    weights: dict[str, float] | None = None,
) -> float:
    """Combined live signal strength on a 0..100+ scale.

    Caps at 100 unless concurrence boost is in effect, then can exceed
    (which is what we want — multi-strategy agreement is genuinely
    better than any single max-confluence signal).
    """
    sw = strategy_weight(strategy_name, weights)
    cm = concurrence_multiplier(concurrence)
    return max(0.0, inside_score) * sw * cm


def reset_cache() -> None:
    _WEIGHT_CACHE.clear()
