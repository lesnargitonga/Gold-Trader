"""Macro-conditioned features for the pattern miner.

Augments the boolean :class:`FeatureMatrix` produced by
:mod:`gold_trader.research.features` with daily macro-regime features
joined as-of into intraday gold bars (last-known-value, no lookahead).

All features are boolean and prefixed ``macro_`` so they are easy to
distinguish in mining output.

Series (when available in the MacroFrame; missing series are skipped):

* ``us10y``      — 10y nominal yield level + 5d change tertiles
* ``real10y``    — 10y real (TIPS) yield + 5d change tertiles
* ``bei10``      — 10y breakeven inflation level + 5d change tertiles
* ``vix``        — equity vol regime (low / mid / high)
* ``dxy``        — broad dollar trend over 5d
* ``fedfunds``   — fed funds direction over 60d
* ``wti``        — crude oil 5d change
* ``usdcny``     — yuan strength over 20d (gold's largest physical buyer)
* ``spx``        — S&P risk-on/off 5d
* ``us10y - us2y`` — yield curve slope sign

Tertile thresholds are computed *historically* from the macro series itself
(across all dates, not just the bar window) — this is fine because macro
series are public daily data, not a leakage source.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Sequence

from ..data.macro import MacroFrame, MacroSeries
from ..models import MarketBar
from .features import FeatureMatrix


def _series_tertile_cuts(series: MacroSeries) -> tuple[float, float] | None:
    """Compute (lo, hi) cut points for tertile bucketing of a series."""
    vals = sorted(p.value for p in series.points)
    if len(vals) < 9:
        return None
    lo = vals[len(vals) // 3]
    hi = vals[(2 * len(vals)) // 3]
    if lo == hi:
        return None
    return lo, hi


def _change_tertile_cuts(
    series: MacroSeries, lookback_days: int,
) -> tuple[float, float] | None:
    """Compute tertile cuts on the *N-day change* of the series.

    Walks the actual point list (not synthetic dates) — robust to weekends.
    """
    if len(series.points) < lookback_days + 9:
        return None
    times = [p.timestamp for p in series.points]
    diffs: list[float] = []
    for i, p in enumerate(series.points):
        target_t = p.timestamp.replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        from datetime import timedelta
        prior_t = target_t - timedelta(days=lookback_days)
        pos = bisect.bisect_right(times, prior_t) - 1
        if pos < 0:
            continue
        diffs.append(p.value - series.points[pos].value)
    if len(diffs) < 9:
        return None
    diffs.sort()
    lo = diffs[len(diffs) // 3]
    hi = diffs[(2 * len(diffs)) // 3]
    if lo == hi:
        return None
    return lo, hi


def _bucket_3(value: float, cuts: tuple[float, float]) -> int:
    if value <= cuts[0]:
        return 0
    if value <= cuts[1]:
        return 1
    return 2


def add_macro_features(
    fm: FeatureMatrix,
    bars: Sequence[MarketBar],
    macro: MacroFrame,
) -> FeatureMatrix:
    """Return a new FeatureMatrix with macro-conditioning features merged in.

    The original FeatureMatrix is not mutated.
    """
    n = len(bars)
    if n != fm.bar_count:
        raise ValueError("bars and FeatureMatrix length mismatch")
    new_feats: dict[str, list[bool]] = dict(fm.features)

    def _add_level_buckets(name: str, series: MacroSeries) -> None:
        cuts = _series_tertile_cuts(series)
        if cuts is None:
            return
        buckets = [-1] * n
        for i, b in enumerate(bars):
            v = series.as_of(b.timestamp)
            if v is None:
                continue
            buckets[i] = _bucket_3(v, cuts)
        for k, lab in enumerate(("lo", "mid", "hi")):
            new_feats[f"macro_{name}_{lab}"] = [
                bk == k for bk in buckets
            ]

    def _add_change_buckets(
        name: str, series: MacroSeries, lookback: int,
    ) -> None:
        cuts = _change_tertile_cuts(series, lookback)
        if cuts is None:
            return
        buckets = [-1] * n
        for i, b in enumerate(bars):
            ch = series.change(b.timestamp, lookback)
            if ch is None:
                continue
            buckets[i] = _bucket_3(ch, cuts)
        for k, lab in enumerate(("down", "flat", "up")):
            new_feats[f"macro_{name}_{lookback}d_{lab}"] = [
                bk == k for bk in buckets
            ]

    # ----- level features --------------------------------------------------
    for name in ("us10y", "real10y", "bei10", "vix", "fedfunds"):
        s = macro.get(name)
        if s is not None:
            _add_level_buckets(name, s)

    # ----- N-day-change features ------------------------------------------
    for name, lookback in (
        ("us10y", 5),
        ("real10y", 5),
        ("bei10", 5),
        ("dxy", 5),
        ("dxy", 20),
        ("vix", 5),
        ("spx", 5),
        ("wti", 5),
        ("usdcny", 20),
        ("fedfunds", 60),
    ):
        s = macro.get(name)
        if s is not None:
            _add_change_buckets(name, s, lookback)

    # ----- yield curve (us10y - us2y) sign --------------------------------
    s10 = macro.get("us10y")
    s2 = macro.get("us2y")
    if s10 is not None and s2 is not None:
        inverted = [False] * n
        steep = [False] * n
        for i, b in enumerate(bars):
            v10 = s10.as_of(b.timestamp)
            v2 = s2.as_of(b.timestamp)
            if v10 is None or v2 is None:
                continue
            spread = v10 - v2
            if spread < 0:
                inverted[i] = True
            elif spread > 1.0:
                steep[i] = True
        new_feats["macro_curve_inverted"] = inverted
        new_feats["macro_curve_steep"] = steep

    # ----- real-yield falling = gold tailwind -----------------------------
    real = macro.get("real10y")
    if real is not None:
        falling = [False] * n
        rising = [False] * n
        for i, b in enumerate(bars):
            ch = real.change(b.timestamp, 20)
            if ch is None:
                continue
            if ch < -0.10:    # >10 bps drop over 20 days
                falling[i] = True
            elif ch > 0.10:
                rising[i] = True
        new_feats["macro_real10y_falling_20d"] = falling
        new_feats["macro_real10y_rising_20d"] = rising

    # ----- DXY-down + real-yield-down compound (the textbook gold setup) --
    dxy = macro.get("dxy")
    if dxy is not None and real is not None:
        gold_tail = [False] * n
        for i, b in enumerate(bars):
            d_dxy = dxy.change(b.timestamp, 5)
            d_real = real.change(b.timestamp, 5)
            if d_dxy is None or d_real is None:
                continue
            if d_dxy < 0 and d_real < 0:
                gold_tail[i] = True
        new_feats["macro_gold_tailwind_5d"] = gold_tail

    # Sanity: all vectors length n.
    for name, vec in new_feats.items():
        assert len(vec) == n, f"feature {name} length mismatch"

    return FeatureMatrix(bar_count=n, features=new_feats)
