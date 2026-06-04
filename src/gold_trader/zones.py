"""Strategy-zone abstraction.

A *zone* is any horizontal price area a strategy considers actionable —
FVG boxes, IFVG inversion levels, divergence pivot prices, breakout
levels, swing lows/highs.  The same Zone shape powers:

- the multi-timeframe live chart overlay (UI Live tab)
- the confluence scorer (combine zones across strategies + TFs)
- the probability slicer (tag trades by which zone fired)

A Zone has a *kind* (string), a price band (top, bot — equal for a level),
a time band (t0, t1 — t1 is None while open), a side bias, and a status
(``pending`` | ``active`` | ``mitigated`` | ``invalidated``).

Every strategy that wants to be on the chart implements
``zones(bars, lookback) -> list[Zone]`` (return all *currently relevant*
zones, recomputed each call — no incremental state, easy to test).

This module also defines the canonical detectors so multiple strategies
can share them: FVG, swing pivots, RSI divergence pivots, prior-day
high/low, asian range, etc.  Strategies are thin wrappers that turn the
shared detectors into their family-specific zone sets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from .models import MarketBar


@dataclass(frozen=True)
class Zone:
    kind: str            # "fvg_bull" | "fvg_bear" | "ifvg_bull" | "ifvg_bear"
                         # | "swing_high" | "swing_low" | "div_pivot_bull" | "div_pivot_bear"
                         # | "asian_high" | "asian_low" | "pdh" | "pdl"
    top: float
    bot: float           # == top for a level (line)
    t0: datetime
    t1: datetime | None = None  # None => still open / unbounded
    side: str = "neutral"       # "long" | "short" | "neutral"
    status: str = "active"       # "pending" | "active" | "mitigated" | "invalidated"
    score: float = 0.0           # quality score, family-defined; >0 = stronger
    reason: str = ""
    family: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "top": self.top,
            "bot": self.bot,
            "t0": self.t0.isoformat() if self.t0 else None,
            "t1": self.t1.isoformat() if self.t1 else None,
            "side": self.side,
            "status": self.status,
            "score": self.score,
            "reason": self.reason,
            "family": self.family,
        }

    def contains_price(self, price: float) -> bool:
        lo, hi = (self.bot, self.top) if self.bot <= self.top else (self.top, self.bot)
        return lo <= price <= hi

    def is_level(self) -> bool:
        return self.top == self.bot


# ---------------------------------------------------------------------------
# Canonical detectors (no lookahead).
# ---------------------------------------------------------------------------


def find_fvgs(bars: Sequence[MarketBar], lookback: int = 200) -> list[Zone]:
    """3-bar Fair-Value-Gap detector.

    Bullish FVG: bars[i-2].high < bars[i].low.  Box = [bars[i-2].high,
    bars[i].low].
    Bearish FVG: bars[i-2].low > bars[i].high.  Box = [bars[i].high,
    bars[i-2].low].

    A FVG is *mitigated* once a later bar's wick trades back into the box;
    after full traversal it's *invalidated*.  Returns one Zone per FVG with
    status reflecting the latest closed bar.
    """
    out: list[Zone] = []
    if len(bars) < 3:
        return out
    start = max(2, len(bars) - lookback)
    for i in range(start, len(bars)):
        b0, b1, b2 = bars[i - 2], bars[i - 1], bars[i]
        # Bullish FVG
        if b0.high < b2.low:
            top = b2.low
            bot = b0.high
            kind = "fvg_bull"
            side = "long"
            status = _fvg_status(bars, i, top=top, bot=bot, side="long")
            out.append(Zone(
                kind=kind, top=top, bot=bot,
                t0=b0.timestamp, t1=None,
                side=side, status=status,
                score=(top - bot) / max(b1.close, 1e-9),
                reason="3-bar FVG (bullish)",
                family="fvg",
            ))
        # Bearish FVG
        if b0.low > b2.high:
            top = b0.low
            bot = b2.high
            kind = "fvg_bear"
            side = "short"
            status = _fvg_status(bars, i, top=top, bot=bot, side="short")
            out.append(Zone(
                kind=kind, top=top, bot=bot,
                t0=b0.timestamp, t1=None,
                side=side, status=status,
                score=(top - bot) / max(b1.close, 1e-9),
                reason="3-bar FVG (bearish)",
                family="fvg",
            ))
    return out


def _fvg_status(
    bars: Sequence[MarketBar], created_index: int,
    top: float, bot: float, side: str,
) -> str:
    """`active` if untouched, `mitigated` if any later bar's wick entered the
    box, `invalidated` if a later bar fully traversed it."""
    after = bars[created_index + 1:]
    touched = False
    for b in after:
        if b.low <= top and b.high >= bot:  # wick entered the box
            touched = True
            if side == "long" and b.close < bot:
                return "invalidated"
            if side == "short" and b.close > top:
                return "invalidated"
    return "mitigated" if touched else "active"


def find_inversion_fvgs(bars: Sequence[MarketBar], lookback: int = 200) -> list[Zone]:
    """Inverted FVG: a bullish FVG that gets violated by a strong close
    *below* its bottom becomes a bearish IFVG zone (resistance), and the
    mirror for the other side. The original gap zone is "inverted" — flipped
    from support to resistance or vice-versa."""
    out: list[Zone] = []
    fvgs = find_fvgs(bars, lookback=lookback)
    bar_index_by_ts = {b.timestamp: i for i, b in enumerate(bars)}
    for fvg in fvgs:
        i0 = bar_index_by_ts.get(fvg.t0)
        if i0 is None:
            continue
        # Look for inversion: a close strictly past the gap on the opposite side
        for j in range(i0 + 3, len(bars)):
            b = bars[j]
            if fvg.kind == "fvg_bull" and b.close < fvg.bot:
                out.append(Zone(
                    kind="ifvg_bear",
                    top=fvg.top, bot=fvg.bot,
                    t0=b.timestamp, t1=None,
                    side="short",
                    status=_ifvg_status(bars, j, top=fvg.top, bot=fvg.bot, side="short"),
                    score=fvg.score,
                    reason="bullish FVG inverted to bearish",
                    family="ifvg",
                ))
                break
            if fvg.kind == "fvg_bear" and b.close > fvg.top:
                out.append(Zone(
                    kind="ifvg_bull",
                    top=fvg.top, bot=fvg.bot,
                    t0=b.timestamp, t1=None,
                    side="long",
                    status=_ifvg_status(bars, j, top=fvg.top, bot=fvg.bot, side="long"),
                    score=fvg.score,
                    reason="bearish FVG inverted to bullish",
                    family="ifvg",
                ))
                break
    return out


def _ifvg_status(
    bars: Sequence[MarketBar], inverted_index: int,
    top: float, bot: float, side: str,
) -> str:
    after = bars[inverted_index + 1:]
    for b in after:
        if side == "long" and b.close < bot:
            return "invalidated"
        if side == "short" and b.close > top:
            return "invalidated"
    return "active"


def find_swing_pivots(
    bars: Sequence[MarketBar],
    pivot_window: int = 3,
    lookback: int = 200,
) -> list[Zone]:
    """Centred-pivot swing highs / lows. No lookahead: pivot at index i
    requires both side windows to be fully inside the available history."""
    out: list[Zone] = []
    n = len(bars)
    if n < 2 * pivot_window + 1:
        return out
    start = max(pivot_window, n - lookback)
    for i in range(start, n - pivot_window):
        window = bars[i - pivot_window: i + pivot_window + 1]
        center = bars[i]
        if center.high == max(b.high for b in window):
            out.append(Zone(
                kind="swing_high", top=center.high, bot=center.high,
                t0=center.timestamp, t1=None,
                side="short", status="active",
                score=0.0, reason="swing high pivot", family="swings",
            ))
        if center.low == min(b.low for b in window):
            out.append(Zone(
                kind="swing_low", top=center.low, bot=center.low,
                t0=center.timestamp, t1=None,
                side="long", status="active",
                score=0.0, reason="swing low pivot", family="swings",
            ))
    return out


def find_prev_day_levels(bars: Sequence[MarketBar]) -> list[Zone]:
    """Previous-day high/low — defined by the bar's calendar day in UTC.
    Returns the two levels for the most-recently-completed day."""
    if not bars:
        return []
    last_day = bars[-1].timestamp.date()
    prev_day_bars: list[MarketBar] = []
    target_day = None
    for b in reversed(bars):
        d = b.timestamp.date()
        if d == last_day:
            continue
        if target_day is None:
            target_day = d
        if d == target_day:
            prev_day_bars.append(b)
        elif d < target_day:
            break
    if not prev_day_bars:
        return []
    pdh = max(b.high for b in prev_day_bars)
    pdl = min(b.low for b in prev_day_bars)
    t0 = min(b.timestamp for b in prev_day_bars)
    return [
        Zone(kind="pdh", top=pdh, bot=pdh, t0=t0, t1=None,
             side="neutral", status="active", reason="previous day high",
             family="prev_day"),
        Zone(kind="pdl", top=pdl, bot=pdl, t0=t0, t1=None,
             side="neutral", status="active", reason="previous day low",
             family="prev_day"),
    ]


def find_asian_range(bars: Sequence[MarketBar]) -> list[Zone]:
    """Most recent asian-session high/low. ``session`` field on bars must be
    populated (the loaders do this)."""
    asian = [b for b in bars if b.session == "asia"]
    if not asian:
        return []
    # last contiguous asian block
    last_ts = asian[-1].timestamp
    block: list[MarketBar] = []
    for b in reversed(asian):
        if (last_ts - b.timestamp).total_seconds() <= 12 * 3600:
            block.append(b)
        else:
            break
    if not block:
        return []
    high = max(b.high for b in block)
    low = min(b.low for b in block)
    t0 = min(b.timestamp for b in block)
    return [
        Zone(kind="asian_high", top=high, bot=high, t0=t0, t1=None,
             side="neutral", status="active", reason="asian session high",
             family="asian"),
        Zone(kind="asian_low", top=low, bot=low, t0=t0, t1=None,
             side="neutral", status="active", reason="asian session low",
             family="asian"),
    ]


# ---------------------------------------------------------------------------
# Aggregator: returns every zone any registered family produces.
# ---------------------------------------------------------------------------


def all_zones(
    bars: Sequence[MarketBar],
    families: Sequence[str] | None = None,
    lookback: int = 200,
) -> list[Zone]:
    """Union of zones across the requested families.

    families=None means "all". Unknown families are silently skipped so
    callers can pass a free-form CSV.
    """
    enabled = (
        {f.strip().lower() for f in families if f.strip()}
        if families is not None else None
    )
    out: list[Zone] = []

    def _on(name: str) -> bool:
        return enabled is None or name in enabled

    if _on("fvg") or _on("fair_value_gap"):
        out.extend(find_fvgs(bars, lookback=lookback))
    if _on("ifvg") or _on("inversion_fair_value_gap"):
        out.extend(find_inversion_fvgs(bars, lookback=lookback))
    if _on("swings"):
        out.extend(find_swing_pivots(bars, lookback=lookback))
    if _on("prev_day"):
        out.extend(find_prev_day_levels(bars))
    if _on("asian"):
        out.extend(find_asian_range(bars))
    return out


__all__ = [
    "Zone",
    "all_zones",
    "find_asian_range",
    "find_fvgs",
    "find_inversion_fvgs",
    "find_prev_day_levels",
    "find_swing_pivots",
]
