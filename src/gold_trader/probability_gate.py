"""Probability gate — slice-based veto on live entries.

Loads conditional-probability tables produced by
``slice-probabilities`` and decides whether the current market regime
matches a slice with proven edge for the candidate's strategy family.

Used by ``agent-cycle`` after the macro filter, before order placement.

Modes (env ``GOLD_PROBABILITY_GATE``):
    off  / unset  — pass-through, no logging
    soft          — log verdict only, never block
    hard          — block when no qualifying slice matches

When no table exists for the family, the gate always passes through (so the
agent doesn't get crippled by missing data — it logs ``no_table``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .data.macro import MacroFrame
from .models import MarketBar
from .regime import RegimeDetector
from .research.probability_slicer import (
    ProbabilityTable,
    SliceStats,
    lookup_slice_probability,
)

DEFAULT_TABLES_DIR = Path("config/probability_tables")


@dataclass(frozen=True)
class GateVerdict:
    family: str
    verdict: str  # "allow" | "block" | "no_table"
    reason: str
    matched_slice: SliceStats | None
    current_dims: dict[str, str]


def _hour_bucket(ts: datetime) -> str:
    h = ts.hour
    if 0 <= h < 6:
        return "00-06"
    if 6 <= h < 12:
        return "06-12"
    if 12 <= h < 18:
        return "12-18"
    return "18-24"


def _dow(ts: datetime) -> str:
    return ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[ts.weekday()]


def _current_dimensions(
    bars: Sequence[MarketBar],
    side: str,
    macro: MacroFrame | None = None,
    detector: RegimeDetector | None = None,
) -> dict[str, str]:
    detector = detector or RegimeDetector()
    idx = len(bars) - 1
    tags = detector.classify(bars, idx, macro=macro)
    last = bars[idx]
    return {
        "session": last.session,
        "dow": _dow(last.timestamp),
        "hour_bucket": _hour_bucket(last.timestamp),
        "vol_pct": tags.vol_pct,
        "trend": tags.trend,
        "compression": tags.compression,
        "spread": tags.spread,
        "session_vwap": tags.session_vwap,
        "macro_real10y": tags.macro_real10y,
        "macro_dxy": tags.macro_dxy,
        "macro_vix": tags.macro_vix,
        "side": side,
    }


def _load_table(family: str, tables_dir: Path) -> ProbabilityTable | None:
    p = tables_dir / f"{family}.json"
    if not p.is_file():
        return None
    try:
        blob = json.loads(p.read_text())
    except Exception:
        return None
    single = tuple(
        SliceStats(
            key=s["key"],
            dimensions=tuple(s["dimensions"]),
            values=tuple(s["values"]),
            n=s["n"], wins=s["wins"], losses=s["losses"],
            win_rate=s["win_rate"], avg_r=s["avg_r"],
            expectancy=s["expectancy"],
            avg_win_r=s["avg_win_r"], avg_loss_r=s["avg_loss_r"],
            lower_ci_r=s["lower_ci_r"], profit_factor=s["profit_factor"],
        )
        for s in blob.get("single_slices", [])
    )
    pair = tuple(
        SliceStats(
            key=s["key"],
            dimensions=tuple(s["dimensions"]),
            values=tuple(s["values"]),
            n=s["n"], wins=s["wins"], losses=s["losses"],
            win_rate=s["win_rate"], avg_r=s["avg_r"],
            expectancy=s["expectancy"],
            avg_win_r=s["avg_win_r"], avg_loss_r=s["avg_loss_r"],
            lower_ci_r=s["lower_ci_r"], profit_factor=s["profit_factor"],
        )
        for s in blob.get("pair_slices", [])
    )
    return ProbabilityTable(
        family=blob.get("family", family),
        n_total=blob.get("n_total", 0),
        base_win_rate=blob.get("base_win_rate", 0.0),
        base_avg_r=blob.get("base_avg_r", 0.0),
        base_expectancy=blob.get("base_expectancy", 0.0),
        base_profit_factor=blob.get("base_profit_factor", 0.0),
        single_slices=single,
        pair_slices=pair,
    )


def evaluate_probability_gate(
    family: str,
    side: str,
    bars: Sequence[MarketBar],
    *,
    tables_dir: Path = DEFAULT_TABLES_DIR,
    macro: MacroFrame | None = None,
    detector: RegimeDetector | None = None,
    min_n: int = 20,
    min_expectancy_r: float = 0.10,
    min_profit_factor: float = 1.20,
) -> GateVerdict:
    """Return verdict for the candidate trade against the family's table.

    ``min_n`` defaults to 20 (raised from 10 on 2026-05-09): n=5 slices with
    PF=∞ are not edges, they're noise. Any slice below n=20 returns
    ``no_table`` rather than ``allow``.
    """
    if not bars:
        return GateVerdict(family, "no_table", "no bars", None, {})
    table = _load_table(family, tables_dir)
    current = _current_dimensions(bars, side, macro=macro, detector=detector)
    if table is None or table.n_total == 0:
        return GateVerdict(family, "no_table", "no probability table on disk", None, current)
    matched = lookup_slice_probability(
        table,
        current,
        min_n=min_n,
        min_expectancy_r=min_expectancy_r,
        min_profit_factor=min_profit_factor,
    )
    if matched is None:
        return GateVerdict(
            family, "block",
            "no qualifying edge slice for current regime",
            None, current,
        )
    return GateVerdict(
        family, "allow",
        f"slice [{matched.key}] n={matched.n} pf={matched.profit_factor:.2f} "
        f"exp={matched.expectancy:+.3f} lci_r={matched.lower_ci_r:+.3f}",
        matched, current,
    )


__all__ = ["GateVerdict", "evaluate_probability_gate", "DEFAULT_TABLES_DIR"]
