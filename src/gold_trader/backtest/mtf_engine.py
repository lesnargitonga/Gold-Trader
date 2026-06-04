"""MTF-aware backtest engine.

Thin adapter on top of :func:`gold_trader.backtest.engine.run_backtest`.

Design
------
We don't fork the engine.  Instead we wrap any :class:`MTFStrategy` as a
plain :class:`Strategy` whose ``signal_for(bars, index)`` rebinds the
shared :class:`MTFContext` and delegates to ``signal_for_mtf``.  Legacy
strategies (without ``signal_for_mtf``) are passed through unchanged —
the bundle is then just used to provide aligned primary bars.

This keeps ALL hard-won engine behaviour (fills, slippage, stops,
universal score, kill switch, fill-aware stops, RR re-targeting) intact
and applies it identically to MTF strategies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from ..models import BacktestConfig, BacktestResult, MarketBar, TradeSignal
from .engine import EntryPriceResolver, run_backtest
from .htf_indicators import HTFIndicatorCache, build_indicator_cache
from .mtf_context import MTFContext
from ..data.mtf import MTFBundle
from ..strategies.base import Strategy


@dataclass
class _MTFAdapter:
    """Wraps an MTFStrategy as a vanilla Strategy."""
    underlying: object  # has signal_for_mtf
    context: MTFContext
    name: str
    _warmup: int

    def warmup_bars(self) -> int:
        return self._warmup

    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        # Enforce required-HTF warmup: skip bar if any required HTF still warming.
        required = getattr(self.underlying, "required_htf", lambda: ())()
        ctx = self.context.at(index)
        for tf in required:
            if ctx.htf_index(tf) < 0:
                return None
        return self.underlying.signal_for_mtf(bars, index, ctx)


def build_indicator_caches(
    bundle: MTFBundle,
    *,
    fast_period: int = 20,
    slow_period: int = 50,
    atr_period: int = 14,
    swing_left: int = 2,
    swing_right: int = 2,
    trend_lookback: int = 5,
) -> dict[str, HTFIndicatorCache]:
    """Build a default indicator cache for every HTF in the bundle."""
    return {
        tf: build_indicator_cache(
            bundle.htf_bars[tf],
            fast_period=fast_period,
            slow_period=slow_period,
            atr_period=atr_period,
            swing_left=swing_left,
            swing_right=swing_right,
            trend_lookback=trend_lookback,
        )
        for tf in bundle.htf_codes
    }


def run_mtf_backtest(
    bundle: MTFBundle,
    strategy: Strategy,
    config: BacktestConfig,
    *,
    indicators: Mapping[str, HTFIndicatorCache] | None = None,
    entry_price_resolver: EntryPriceResolver | None = None,
) -> BacktestResult:
    """Run a single-strategy backtest with an :class:`MTFBundle`.

    If ``strategy`` implements ``signal_for_mtf`` it is wrapped in an
    adapter that injects an :class:`MTFContext`.  Otherwise the call
    degrades to a vanilla :func:`run_backtest` over the bundle's primary
    bars — this lets legacy strategies coexist with the MTF pipeline.

    ``indicators`` defaults to a full default-parameter cache for every
    HTF in the bundle.  Pass a custom map to override periods.

    ``entry_price_resolver`` is forwarded to the underlying engine and
    is typically constructed via
    :func:`gold_trader.backtest.ltf_trigger.make_ltf_entry_resolver`.
    """
    if indicators is None:
        indicators = build_indicator_caches(bundle)

    if hasattr(strategy, "signal_for_mtf"):
        ctx = MTFContext(bundle=bundle, indicators=indicators)
        adapter = _MTFAdapter(
            underlying=strategy,
            context=ctx,
            name=getattr(strategy, "name", "mtf_strategy"),
            _warmup=strategy.warmup_bars(),
        )
        return run_backtest(
            list(bundle.primary_bars), adapter, config,
            entry_price_resolver=entry_price_resolver,
        )

    return run_backtest(
        list(bundle.primary_bars), strategy, config,
        entry_price_resolver=entry_price_resolver,
    )


__all__ = ["run_mtf_backtest", "build_indicator_caches"]
