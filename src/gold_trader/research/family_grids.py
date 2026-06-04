"""Registry mapping each strategy family name to its default parameter grid
and to a strategy factory callable.

Lets callers (champion selector, holdout-eval CLI, web UI Lab tab) ask for
"give me everything I need to evaluate family X" without restating the giant
elif chain that lives in cli.py.

Families that need extra data (DXY merged into the CSV, or a macro cache
directory) are marked accordingly so the caller can decide whether to skip
them.  The factory closure for those families is built lazily by the caller
because it needs the extra context (e.g. the macro frame).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from . import experiments as _exp
from ._factory_registry import make_strategy
from ..strategies.base import Strategy


# Family name -> grid-factory callable.  Each callable returns a fresh
# parameter grid (list of frozen dataclass instances).
SELF_CONTAINED_FAMILIES: dict[str, Callable[[], Sequence[Any]]] = {
    "liquidity_sweep":           _exp.default_liquidity_grid,
    "compression_breakout":      _exp.default_compression_grid,
    "asian_range_breakout":      _exp.default_asian_range_grid,
    "london_breakout":           _exp.default_london_breakout_grid,
    "trend_pullback":            _exp.default_trend_pullback_grid,
    "ny_session_breakout":       _exp.default_ny_session_breakout_grid,
    "momentum_burst":            _exp.default_momentum_burst_grid,
    "previous_day_breakout":     _exp.default_previous_day_breakout_grid,
    "opening_range_breakout":    _exp.default_opening_range_breakout_grid,
    "asian_range_fade":          _exp.default_asian_range_fade_grid,
    "fair_value_gap":            _exp.default_fair_value_gap_grid,
    "inversion_fair_value_gap":  _exp.default_inversion_fair_value_gap_grid,
    "rsi_divergence":            _exp.default_rsi_divergence_grid,
    "ny_close_compression":      _exp.default_ny_close_compression_grid,
    "session_continuation":      _exp.default_session_continuation_grid,
}

# Families that require an external data context (DXY-merged CSV or macro
# frame).  Listed for completeness; caller wires them up explicitly.
EXTERNAL_DATA_FAMILIES: dict[str, str] = {
    "dxy_lead_lag":                "requires DXY column merged into CSV",
    "real_yield_reversal":         "requires macro cache directory",
    "timed_horizon_macro_regime":  "requires macro cache directory (real10y/dxy/vix)",
}

# Macro-cache-using families.  Caller must load a MacroFrame and pass it to
# ``family_spec_with_macro``.
MACRO_FAMILIES: dict[str, Callable[[], Sequence[Any]]] = {
    "real_yield_reversal":         _exp.default_real_yield_reversal_grid,
    "timed_horizon_macro_regime":  _exp.default_timed_horizon_macro_regime_grid,
}


@dataclass(frozen=True)
class FamilySpec:
    name: str
    grid: Sequence[Any]
    factory: Callable[[Any], Strategy]


def family_spec(family: str) -> FamilySpec:
    """Return grid + factory for a self-contained family.

    Raises KeyError for unknown / external-data families.
    """
    if family not in SELF_CONTAINED_FAMILIES:
        raise KeyError(
            f"family {family!r} is not in the self-contained registry; "
            f"available: {sorted(SELF_CONTAINED_FAMILIES)}"
        )
    grid = SELF_CONTAINED_FAMILIES[family]()
    factory = lambda params, _f=family: make_strategy(_f, params)
    return FamilySpec(name=family, grid=list(grid), factory=factory)


def family_spec_with_macro(family: str, macro_frame: Any) -> FamilySpec:
    """Return grid + factory for a macro-cache-using family.

    The factory closes over *macro_frame* so the caller can use the same
    ``FamilySpec`` shape as for self-contained families.

    Raises KeyError for non-macro families.
    """
    if family not in MACRO_FAMILIES:
        raise KeyError(
            f"family {family!r} is not a macro-cache family; "
            f"available: {sorted(MACRO_FAMILIES)}"
        )
    grid = MACRO_FAMILIES[family]()
    factory = lambda params, _f=family, _m=macro_frame: make_strategy(
        _f, params, macro=_m,
    )
    return FamilySpec(name=family, grid=list(grid), factory=factory)


def all_self_contained_families() -> list[str]:
    return sorted(SELF_CONTAINED_FAMILIES.keys())


def all_macro_families() -> list[str]:
    return sorted(MACRO_FAMILIES.keys())
