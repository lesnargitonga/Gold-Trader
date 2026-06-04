"""Confluence scorer.

Combine multiple zones (across families and timeframes) that overlap in
price into a single ``ConfluencePoint``. The point's confidence is the
union of:

- number of distinct zone *kinds* agreeing on the same side
- diversity of source families (FVG + swing + prev-day ≫ three FVGs)
- timeframe weight (D1 > H4 > H1 > M15 > M5 > M1)
- recency penalty (zones older than ``max_age_bars`` get a half-life decay)
- mitigation discount (mitigated zones contribute half)

Returns ``ConfluencePoint(price_top, price_bot, side, score, contributors)``
sorted by score descending. The live agent uses these as a probabilistic
prior multiplier when a strategy candidate fires near a confluent zone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from .zones import Zone

# Timeframe weight for the confluence score.  Higher TF = stronger.
DEFAULT_TF_WEIGHT: dict[int, float] = {
    1: 0.30,
    5: 0.45,
    15: 0.65,
    60: 0.85,
    240: 1.10,
    1440: 1.40,
    10080: 1.70,
}


@dataclass(frozen=True)
class ConfluencePoint:
    price_top: float
    price_bot: float
    side: str  # "long" | "short" | "neutral"
    score: float
    contributors: tuple[Zone, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "price_top": self.price_top,
            "price_bot": self.price_bot,
            "side": self.side,
            "score": self.score,
            "contributors": [z.to_dict() for z in self.contributors],
            "n_contributors": len(self.contributors),
        }


def _zones_overlap(a: Zone, b: Zone, tolerance: float) -> bool:
    a_lo, a_hi = (a.bot, a.top) if a.bot <= a.top else (a.top, a.bot)
    b_lo, b_hi = (b.bot, b.top) if b.bot <= b.top else (b.top, b.bot)
    a_lo -= tolerance
    a_hi += tolerance
    return not (a_hi < b_lo or b_hi < a_lo)


def _merge_band(zones: Sequence[Zone]) -> tuple[float, float]:
    tops = [max(z.top, z.bot) for z in zones]
    bots = [min(z.top, z.bot) for z in zones]
    return max(tops), min(bots)


def _zone_weight(
    zone: Zone,
    timeframe_minutes: int,
    tf_weight: dict[int, float],
    now: datetime | None,
    max_age_bars: int,
) -> float:
    base = tf_weight.get(timeframe_minutes, 0.65)
    if zone.status == "mitigated":
        base *= 0.5
    if zone.status == "invalidated":
        return 0.0
    if now is not None and zone.t0 is not None and timeframe_minutes > 0:
        age_minutes = max(0.0, (now - zone.t0).total_seconds() / 60.0)
        age_bars = age_minutes / timeframe_minutes
        if age_bars > max_age_bars:
            # half-life decay past the cutoff
            decay = 0.5 ** ((age_bars - max_age_bars) / max_age_bars)
            base *= decay
    return base


def _side_consensus(zones: Sequence[Zone]) -> str:
    longs = sum(1 for z in zones if z.side == "long")
    shorts = sum(1 for z in zones if z.side == "short")
    if longs > shorts:
        return "long"
    if shorts > longs:
        return "short"
    return "neutral"


def score_confluence(
    zones_by_tf: dict[int, Sequence[Zone]],
    *,
    tolerance: float = 0.50,
    tf_weight: dict[int, float] | None = None,
    now: datetime | None = None,
    max_age_bars: int = 50,
    min_contributors: int = 2,
) -> list[ConfluencePoint]:
    """Cluster overlapping zones across timeframes into confluence points.

    Parameters
    ----------
    zones_by_tf
        Mapping of timeframe-minutes -> list of Zone for that TF.
    tolerance
        Price tolerance (in price units) used when merging overlapping
        zones; should be a small ATR-derived buffer.
    tf_weight
        Optional override for default timeframe weights.
    now
        Reference timestamp for the recency decay.  Defaults to the latest
        ``Zone.t0`` if None.
    max_age_bars
        Number of bars after which a zone starts decaying.
    min_contributors
        Minimum distinct contributing zones required to emit a point.
    """
    tf_weight = tf_weight or DEFAULT_TF_WEIGHT
    flat: list[tuple[Zone, int]] = []
    for tf, zones in zones_by_tf.items():
        for z in zones:
            flat.append((z, tf))
    if not flat:
        return []

    if now is None:
        now = max(z.t0 for z, _ in flat if z.t0 is not None)

    # union-find clustering by price-band overlap
    parent = list(range(len(flat)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(len(flat)):
        for j in range(i + 1, len(flat)):
            zi, _ = flat[i]
            zj, _ = flat[j]
            if zi.side != zj.side and zi.side != "neutral" and zj.side != "neutral":
                continue
            if _zones_overlap(zi, zj, tolerance):
                union(i, j)

    clusters: dict[int, list[tuple[Zone, int]]] = {}
    for i, (z, tf) in enumerate(flat):
        clusters.setdefault(find(i), []).append((z, tf))

    out: list[ConfluencePoint] = []
    for members in clusters.values():
        if len(members) < min_contributors:
            continue
        zones = [z for z, _ in members]
        # deduplicate identical kinds to count *family* diversity
        family_diversity = len({z.family for z in zones})
        kinds_diversity = len({z.kind for z in zones})
        weight = sum(
            _zone_weight(z, tf, tf_weight, now, max_age_bars)
            for z, tf in members
        )
        side = _side_consensus(zones)
        if weight <= 0.0:
            continue
        score = weight * (1.0 + 0.25 * (family_diversity - 1)) * (1.0 + 0.10 * (kinds_diversity - 1))
        top, bot = _merge_band(zones)
        out.append(ConfluencePoint(
            price_top=top,
            price_bot=bot,
            side=side,
            score=score,
            contributors=tuple(zones),
        ))

    out.sort(key=lambda c: c.score, reverse=True)
    return out


__all__ = [
    "ConfluencePoint",
    "DEFAULT_TF_WEIGHT",
    "score_confluence",
]
