"""Multi-timeframe alignment infrastructure.

Provides :class:`MTFBundle` — a container of bar series across multiple
timeframes plus a pre-computed alignment index that maps each primary-bar
position to the latest *closed* higher-timeframe bar at-or-before it.

No-look-ahead invariant
-----------------------
For an HTF bar starting at ``t_h`` with timeframe duration ``Δ``, that bar
is only *known* (closed) at time ``t_h + Δ``.  Therefore for primary bar
at time ``t_p``, the latest usable HTF index ``j`` satisfies::

    htf[j].timestamp + Δ  <=  t_p

If no such bar exists (insufficient history), the alignment returns
``-1``.  Callers MUST treat ``-1`` as "HTF unavailable, skip signal".
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ..models import MarketBar
from .csv_loader import load_bars_from_csv


# Canonical timeframe codes used throughout the system.  Minutes per bar.
TF_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "60m": 60,
    "240m": 240,
    "1440m": 1440,
}


def tf_duration(tf: str) -> timedelta:
    if tf not in TF_MINUTES:
        raise ValueError(f"unknown timeframe code: {tf!r}")
    return timedelta(minutes=TF_MINUTES[tf])


def build_alignment(
    primary_bars: Sequence[MarketBar],
    htf_bars: Sequence[MarketBar],
    htf_tf: str,
) -> list[int]:
    """Return a list ``align`` of length ``len(primary_bars)`` where
    ``align[i]`` is the index ``j`` into ``htf_bars`` of the most recent
    *closed* HTF bar at-or-before ``primary_bars[i].timestamp``.

    ``-1`` is returned where no closed HTF bar exists yet (warmup).
    """
    if not primary_bars:
        return []
    if not htf_bars:
        return [-1] * len(primary_bars)

    delta = tf_duration(htf_tf)
    # Effective "available at" time of htf_bars[j] is htf_bars[j].timestamp + delta.
    # We want the largest j such that available_at[j] <= primary_ts.
    avail = [b.timestamp + delta for b in htf_bars]

    align: list[int] = []
    # avail is sorted (htf bars chronological).  Use bisect_right to find
    # insertion point: number of elements <= primary_ts is bisect_right(avail, ts).
    # But bisect_right on datetime requires the list to be sorted (it is).
    for bar in primary_bars:
        idx = bisect_right(avail, bar.timestamp) - 1
        align.append(idx if idx >= 0 else -1)
    return align


@dataclass(frozen=True)
class MTFBundle:
    """Aligned multi-timeframe bar bundle.

    ``primary_tf`` is the timeframe of ``primary_bars`` (the timeframe the
    backtest engine iterates over).  ``htf_bars`` maps each higher
    timeframe code to its bar series.  ``alignments`` maps each HTF code
    to a list of length ``len(primary_bars)`` of HTF indices (or -1).
    """

    primary_tf: str
    primary_bars: tuple[MarketBar, ...]
    htf_bars: Mapping[str, tuple[MarketBar, ...]] = field(default_factory=dict)
    alignments: Mapping[str, tuple[int, ...]] = field(default_factory=dict)

    def htf_index_at(self, tf: str, primary_index: int) -> int:
        """Latest closed HTF bar index at primary position; -1 if warming."""
        align = self.alignments.get(tf)
        if align is None:
            raise KeyError(f"timeframe {tf!r} not in bundle")
        if primary_index < 0 or primary_index >= len(align):
            raise IndexError(primary_index)
        return align[primary_index]

    def htf_bar_at(self, tf: str, primary_index: int) -> MarketBar | None:
        idx = self.htf_index_at(tf, primary_index)
        if idx < 0:
            return None
        return self.htf_bars[tf][idx]

    def htf_slice(self, tf: str, primary_index: int) -> tuple[MarketBar, ...]:
        """All HTF bars closed at-or-before this primary index (inclusive)."""
        idx = self.htf_index_at(tf, primary_index)
        if idx < 0:
            return ()
        return self.htf_bars[tf][: idx + 1]

    @property
    def htf_codes(self) -> tuple[str, ...]:
        return tuple(self.htf_bars.keys())


def build_mtf_bundle(
    primary_tf: str,
    primary_bars: Sequence[MarketBar],
    htf_bars_by_tf: Mapping[str, Sequence[MarketBar]],
) -> MTFBundle:
    """Construct an :class:`MTFBundle` from already-loaded bar series."""
    if primary_tf not in TF_MINUTES:
        raise ValueError(f"unknown primary timeframe: {primary_tf!r}")
    primary_min = TF_MINUTES[primary_tf]

    aligns: dict[str, tuple[int, ...]] = {}
    htf_frozen: dict[str, tuple[MarketBar, ...]] = {}
    for tf, bars in htf_bars_by_tf.items():
        if tf not in TF_MINUTES:
            raise ValueError(f"unknown HTF: {tf!r}")
        if TF_MINUTES[tf] <= primary_min:
            raise ValueError(
                f"HTF {tf!r} is not strictly higher than primary {primary_tf!r}"
            )
        aligns[tf] = tuple(build_alignment(primary_bars, bars, tf))
        htf_frozen[tf] = tuple(bars)

    return MTFBundle(
        primary_tf=primary_tf,
        primary_bars=tuple(primary_bars),
        htf_bars=htf_frozen,
        alignments=aligns,
    )


def load_mtf_bundle_from_dir(
    directory: str | Path,
    primary_tf: str,
    htf_tfs: Iterable[str],
    *,
    filename_pattern: str = "xauusd_5y_{tf}.csv",
    time_lo: datetime | None = None,
    time_hi: datetime | None = None,
) -> MTFBundle:
    """Load primary + HTF bar files from ``directory`` and assemble a bundle.

    The filenames are derived from ``filename_pattern`` (default matches
    the project convention ``xauusd_5y_{tf}.csv``).  Optional ``time_lo``
    / ``time_hi`` restrict every series to ``time_lo <= ts <= time_hi``.
    """
    base = Path(directory)
    primary_path = base / filename_pattern.format(tf=primary_tf)
    primary = load_bars_from_csv(primary_path)
    if time_lo is not None or time_hi is not None:
        primary = [
            b for b in primary
            if (time_lo is None or b.timestamp >= time_lo)
            and (time_hi is None or b.timestamp <= time_hi)
        ]

    htf_data: dict[str, Sequence[MarketBar]] = {}
    for tf in htf_tfs:
        path = base / filename_pattern.format(tf=tf)
        bars = load_bars_from_csv(path)
        if time_lo is not None or time_hi is not None:
            # Pad HTF window so the first primary bar still has closed HTF
            # context once it warms up.  We add 2 * tf_duration of pad.
            pad = 2 * tf_duration(tf)
            lo = (time_lo - pad) if time_lo is not None else None
            hi = time_hi
            bars = [
                b for b in bars
                if (lo is None or b.timestamp >= lo)
                and (hi is None or b.timestamp <= hi)
            ]
        htf_data[tf] = bars

    return build_mtf_bundle(primary_tf, primary, htf_data)


__all__ = [
    "TF_MINUTES",
    "tf_duration",
    "build_alignment",
    "MTFBundle",
    "build_mtf_bundle",
    "load_mtf_bundle_from_dir",
]
