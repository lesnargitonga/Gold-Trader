"""Macro-regime decision filter.

A pluggable boolean gate that any strategy or the agent-cycle can apply
to a fresh signal:  *should this trade be allowed under the current
macro regime?*

The defaults encode the empirical findings from the 2026-05-08
macro-augmented mining sweep:

* **Bullish regime for longs**: real10y falling 5d, VIX 5d-change
  calm, DXY consolidating over 20d.  Mining showed avg_R = +1.35 to
  +1.89 in this regime; trades opened against this regime had
  avg_R close to zero or negative.
* **Stagflation block**: bei10 in top tercile + real10y in bottom
  tercile is gold-bearish for longs (avg_R = -3.44 in mining).

The filter does NOT generate signals — it only accepts or rejects
signals from other strategies.  Use it as a final-mile gate so the
agent doesn't take longs in regimes where the data shows the edge
disappears.

Operational mode
----------------
The filter has three configurable verdicts:

* ``allow`` — regime supportive of the signal direction.
* ``allow_with_warning`` — regime is neutral; trade with caution.
* ``block`` — regime contradicts the signal direction.

Wire into agent-cycle via ``GOLD_MACRO_FILTER`` env var:

* unset / ``off``  : filter disabled (current default).
* ``soft``         : log the verdict but never block.
* ``hard``         : skip orders the filter blocks.

Soft mode is the safe rollout — collect telemetry on which trades
would have been blocked, then promote to hard once the data backs it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .data.macro import MacroFrame
from .models import Side


Verdict = Literal["allow", "allow_with_warning", "block"]


@dataclass(frozen=True)
class FilterDecision:
    verdict: Verdict
    reason: str
    regime_tags: tuple[str, ...]


@dataclass(frozen=True)
class MacroDecisionFilter:
    """Boolean gate based on macro regime at signal time."""

    macro: MacroFrame

    real_yield_lookback_days: int = 5
    """Real-yield direction lookback."""

    vix_lookback_days: int = 5

    dxy_lookback_days: int = 20

    block_stagflation_longs: bool = True
    """If True and bei10 in top tercile AND real10y in bottom tercile,
    block long signals."""

    block_real_yield_rising_longs: bool = True
    """Block longs when real10y rose >5bps over lookback (gold headwind)."""
    real_yield_rising_block_bps: float = 5.0

    block_dxy_strong_longs: bool = True
    """Block longs when DXY rose >1% over 20d (dollar trend up = gold down)."""
    dxy_rising_block_pct: float = 1.0

    block_real_yield_falling_shorts: bool = True
    """Symmetric: block shorts when real yields are dropping."""

    warn_unsupportive: bool = True
    """If True, signals not blocked but in a non-supportive regime get
    ``allow_with_warning`` instead of ``allow``."""

    def evaluate(
        self,
        side: Side,
        timestamp: datetime,
    ) -> FilterDecision:
        m = self.macro
        tags: list[str] = []

        real = m.get("real10y")
        dxy = m.get("dxy")
        bei = m.get("bei10")

        d_real_bps = None
        if real is not None:
            d_real = real.change(timestamp, lookback_days=self.real_yield_lookback_days)
            if d_real is not None:
                d_real_bps = d_real * 100.0
                if d_real_bps > 0:
                    tags.append("real10y_rising")
                elif d_real_bps < 0:
                    tags.append("real10y_falling")

        d_dxy_pct = None
        if dxy is not None:
            d_dxy = dxy.pct_change(timestamp, lookback_days=self.dxy_lookback_days)
            if d_dxy is not None:
                d_dxy_pct = d_dxy * 100.0
                if d_dxy_pct > self.dxy_rising_block_pct:
                    tags.append("dxy_strong")
                elif d_dxy_pct < -self.dxy_rising_block_pct:
                    tags.append("dxy_weak")
                elif abs(d_dxy_pct) <= self.dxy_rising_block_pct / 2.0:
                    tags.append("dxy_flat")

        # Stagflation regime detection.
        stagflation = False
        if bei is not None and real is not None:
            bv = bei.as_of(timestamp)
            rv = real.as_of(timestamp)
            if bv is not None and rv is not None:
                bei_vals = sorted(p.value for p in bei.points)
                real_vals = sorted(p.value for p in real.points)
                if len(bei_vals) >= 9 and len(real_vals) >= 9:
                    bei_hi = bei_vals[(2 * len(bei_vals)) // 3]
                    real_lo = real_vals[len(real_vals) // 3]
                    if bv >= bei_hi and rv <= real_lo:
                        stagflation = True
                        tags.append("stagflation")

        # Apply blocking rules.
        if side == Side.LONG:
            if self.block_stagflation_longs and stagflation:
                return FilterDecision(
                    "block",
                    "Stagflation regime (bei10 hi + real10y lo) — long blocked.",
                    tuple(tags),
                )
            if (
                self.block_real_yield_rising_longs
                and d_real_bps is not None
                and d_real_bps >= self.real_yield_rising_block_bps
            ):
                return FilterDecision(
                    "block",
                    f"real10y rose {d_real_bps:.1f}bps over "
                    f"{self.real_yield_lookback_days}d — long headwind.",
                    tuple(tags),
                )
            if (
                self.block_dxy_strong_longs
                and d_dxy_pct is not None
                and d_dxy_pct >= self.dxy_rising_block_pct
            ):
                return FilterDecision(
                    "block",
                    f"DXY rose {d_dxy_pct:.2f}% over "
                    f"{self.dxy_lookback_days}d — long headwind.",
                    tuple(tags),
                )

        elif side == Side.SHORT:
            if (
                self.block_real_yield_falling_shorts
                and d_real_bps is not None
                and d_real_bps <= -self.real_yield_rising_block_bps
            ):
                return FilterDecision(
                    "block",
                    f"real10y fell {abs(d_real_bps):.1f}bps over "
                    f"{self.real_yield_lookback_days}d — short headwind.",
                    tuple(tags),
                )

        # Not blocked — supportive or neutral?
        supportive = False
        if side == Side.LONG:
            if d_real_bps is not None and d_real_bps < 0:
                supportive = True
            if d_dxy_pct is not None and d_dxy_pct <= -self.dxy_rising_block_pct / 2.0:
                supportive = True
        else:  # SHORT
            if d_real_bps is not None and d_real_bps > 0:
                supportive = True
            if d_dxy_pct is not None and d_dxy_pct >= self.dxy_rising_block_pct / 2.0:
                supportive = True

        if supportive:
            return FilterDecision(
                "allow", "Regime supportive of signal direction.", tuple(tags),
            )
        if self.warn_unsupportive:
            return FilterDecision(
                "allow_with_warning",
                "Regime neutral — no macro tailwind in signal direction.",
                tuple(tags),
            )
        return FilterDecision("allow", "Macro filter passive.", tuple(tags))
