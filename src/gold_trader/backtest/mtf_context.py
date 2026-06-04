"""Per-bar MTF query interface passed to multi-timeframe strategies.

A :class:`MTFContext` is a lightweight read-only view on an
:class:`gold_trader.data.mtf.MTFBundle` plus pre-built
:class:`gold_trader.backtest.htf_indicators.HTFIndicatorCache` per HTF.
Strategies call methods on it during ``signal_for_mtf`` to query HTF
state at the current primary index.

All queries respect the strict no-look-ahead invariant: the caller cannot
observe an HTF bar until that bar's *close* has elapsed before the
current primary-bar timestamp.

Backwards compatibility
-----------------------
Strategies that don't need MTF context continue to use the original
:class:`gold_trader.strategies.base.Strategy` Protocol (``signal_for``).
The MTF engine adapts these by ignoring the ``mtf`` argument.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence, runtime_checkable

from ..data.mtf import MTFBundle
from ..models import MarketBar, TradeSignal
from .htf_indicators import HTFIndicatorCache, SwingPoint


@dataclass(frozen=True)
class MTFContext:
    """Read-only per-bar MTF query handle.

    Construct one of these per backtest run; the ``primary_index`` is
    rebound by the engine each iteration via :meth:`at`.
    """

    bundle: MTFBundle
    indicators: Mapping[str, HTFIndicatorCache]
    primary_index: int = 0

    # ------------------------------------------------------------------
    # Position re-binding (engine internal use)
    # ------------------------------------------------------------------
    def at(self, primary_index: int) -> "MTFContext":
        """Return a copy with ``primary_index`` rebound — used by the
        engine each iteration."""
        return MTFContext(
            bundle=self.bundle,
            indicators=self.indicators,
            primary_index=primary_index,
        )

    # ------------------------------------------------------------------
    # Generic HTF lookups
    # ------------------------------------------------------------------
    def htf_index(self, tf: str) -> int:
        """Latest closed HTF bar index at the current primary position
        (or -1 if HTF still warming or not in bundle)."""
        if tf not in self.bundle.htf_codes:
            return -1
        return self.bundle.htf_index_at(tf, self.primary_index)

    def htf_bar(self, tf: str) -> MarketBar | None:
        if tf not in self.bundle.htf_codes:
            return None
        return self.bundle.htf_bar_at(tf, self.primary_index)

    def htf_slice(self, tf: str) -> tuple[MarketBar, ...]:
        if tf not in self.bundle.htf_codes:
            return ()
        return self.bundle.htf_slice(tf, self.primary_index)

    def has_htf(self, tf: str) -> bool:
        return tf in self.bundle.htf_codes

    # ------------------------------------------------------------------
    # Cached indicator queries
    # ------------------------------------------------------------------
    def trend(self, tf: str) -> str:
        """One of ``'up' | 'down' | 'flat'`` for the latest closed HTF
        bar; ``'flat'`` if the HTF is still warming."""
        idx = self.htf_index(tf)
        cache = self.indicators.get(tf)
        if cache is None or idx < 0:
            return "flat"
        return cache.trend_at(idx)

    def ema_fast(self, tf: str) -> float | None:
        idx = self.htf_index(tf)
        cache = self.indicators.get(tf)
        if cache is None or idx < 0:
            return None
        return cache.ema_fast_at(idx)

    def ema_slow(self, tf: str) -> float | None:
        idx = self.htf_index(tf)
        cache = self.indicators.get(tf)
        if cache is None or idx < 0:
            return None
        return cache.ema_slow_at(idx)

    def atr(self, tf: str) -> float | None:
        idx = self.htf_index(tf)
        cache = self.indicators.get(tf)
        if cache is None or idx < 0:
            return None
        return cache.atr_at(idx)

    def last_swing_high(self, tf: str) -> SwingPoint | None:
        idx = self.htf_index(tf)
        cache = self.indicators.get(tf)
        if cache is None or idx < 0:
            return None
        return cache.last_confirmed_swing_high(idx)

    def last_swing_low(self, tf: str) -> SwingPoint | None:
        idx = self.htf_index(tf)
        cache = self.indicators.get(tf)
        if cache is None or idx < 0:
            return None
        return cache.last_confirmed_swing_low(idx)


# ---------------------------------------------------------------------------
# Strategy protocol v2
# ---------------------------------------------------------------------------

@runtime_checkable
class MTFStrategy(Protocol):
    """Optional second protocol — strategies that exploit MTF data.

    Any object with this signature also satisfies the original
    :class:`Strategy` Protocol (the engine will call whichever method is
    present).  When both are defined, ``signal_for_mtf`` wins.
    """

    name: str

    def warmup_bars(self) -> int:
        ...

    def required_htf(self) -> tuple[str, ...]:
        """List of HTF codes this strategy will query.  Used by the
        engine to validate the bundle and skip bars where any required
        HTF is still warming."""
        ...

    def signal_for_mtf(
        self,
        bars: Sequence[MarketBar],
        index: int,
        mtf: MTFContext,
    ) -> TradeSignal | None:
        ...


__all__ = ["MTFContext", "MTFStrategy"]
