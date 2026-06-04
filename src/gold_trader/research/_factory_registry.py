"""Strategy factory registry.

Maps family name strings to strategy constructors.  This module is imported
by worker processes in ``parallel_search`` so it must avoid heavy imports at
module level — each function instantiates the strategy on demand.

Why a separate file?
--------------------
Worker functions passed to ``ProcessPoolExecutor`` must be picklable, which
means they must be defined at module level (not as closures inside CLI command
handlers).  The factory functions here are module-level and therefore
picklable.  Workers call ``make_strategy(family, params)`` after receiving
only the small param dataclass via IPC — the large bars array is handled via
the pool initializer.
"""
from __future__ import annotations

from typing import Any


def make_strategy(family_name: str, params: Any, *, macro: Any = None):  # noqa: ANN201
    """Reconstruct a Strategy instance from *family_name* + *params* dataclass.

    All imports are deferred (inside the function) so this can be safely
    called in subprocess workers without triggering top-level import cycles.

    Macro-cache families (``real_yield_reversal``, ``timed_horizon_macro_regime``)
    require ``macro`` to be a populated ``MacroFrame``.  Self-contained
    families ignore ``macro``.
    """
    if family_name == "asian_range_breakout":
        from ..strategies.asian_range_breakout import AsianRangeBreakoutStrategy
        return AsianRangeBreakoutStrategy(
            atr_period=params.atr_period,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
            min_breakout_atr=params.min_breakout_atr,
            min_range_atr=params.min_range_atr,
            min_asian_bars=params.min_asian_bars,
            min_atr_threshold=getattr(params, "min_atr_threshold", 0.0),
        )

    if family_name == "ny_session_breakout":
        from ..strategies.ny_session_breakout import NYSessionBreakoutStrategy
        return NYSessionBreakoutStrategy(
            atr_period=params.atr_period,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
            min_breakout_atr=params.min_breakout_atr,
            min_range_atr=params.min_range_atr,
            min_london_bars=params.min_london_bars,
            entry_end_hour=getattr(params, "entry_end_hour", 21),
            require_asian_alignment=getattr(params, "require_asian_alignment", False),
        )

    if family_name == "london_breakout":
        from ..strategies.london_breakout import LondonBreakoutStrategy
        return LondonBreakoutStrategy(
            opening_range_bars=params.opening_range_bars,
            atr_period=params.atr_period,
            min_breakout_atr=params.min_breakout_atr,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
            min_atr_threshold=getattr(params, "min_atr_threshold", 0.0),
        )

    if family_name == "trend_pullback":
        from ..strategies.trend_pullback import TrendPullbackStrategy
        return TrendPullbackStrategy(
            ema_fast=params.ema_fast,
            ema_slow=params.ema_slow,
            atr_period=params.atr_period,
            trend_strength_min=params.trend_strength_min,
            pullback_tolerance=params.pullback_tolerance,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
        )

    if family_name == "momentum_burst":
        from ..strategies.momentum_burst import MomentumBurstStrategy
        return MomentumBurstStrategy(
            atr_period=params.atr_period,
            min_body_atr=params.min_body_atr,
            body_fraction=params.body_fraction,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
            min_atr_threshold=getattr(params, "min_atr_threshold", 0.0),
        )

    if family_name == "liquidity_sweep":
        from ..strategies.liquidity_sweep import LiquiditySweepStrategy
        return LiquiditySweepStrategy(
            lookback=params.lookback,
            atr_period=params.atr_period,
            min_sweep_atr=params.min_sweep_atr,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
        )

    if family_name == "compression_breakout":
        from ..strategies.compression_breakout import CompressionBreakoutStrategy
        return CompressionBreakoutStrategy(
            breakout_lookback=params.breakout_lookback,
            compression_lookback=params.compression_lookback,
            atr_period=params.atr_period,
            max_compression_atr_ratio=params.max_compression_atr_ratio,
            min_breakout_atr=params.min_breakout_atr,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
            min_atr_threshold=getattr(params, "min_atr_threshold", 0.0),
        )

    if family_name == "previous_day_breakout":
        from ..strategies.previous_day_breakout import PreviousDayBreakoutStrategy
        return PreviousDayBreakoutStrategy(
            atr_period=params.atr_period,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
            min_breakout_atr=params.min_breakout_atr,
            stop_atr_buffer=params.stop_atr_buffer,
        )

    if family_name == "opening_range_breakout":
        from ..strategies.opening_range_breakout import OpeningRangeBreakoutStrategy
        return OpeningRangeBreakoutStrategy(
            opening_range_bars=params.opening_range_bars,
            atr_period=params.atr_period,
            min_breakout_atr=params.min_breakout_atr,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
        )

    if family_name == "asian_range_fade":
        from ..strategies.asian_range_fade import AsianRangeFadeStrategy
        return AsianRangeFadeStrategy(
            atr_period=params.atr_period,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
            min_rejection_atr=params.min_rejection_atr,
            min_range_atr=params.min_range_atr,
            stop_atr_buffer=params.stop_atr_buffer,
        )

    if family_name == "fair_value_gap":
        from ..strategies.fair_value_gap import FairValueGapStrategy
        return FairValueGapStrategy(
            atr_period=params.atr_period,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
            min_gap_atr=params.min_gap_atr,
            fvg_lookback=params.fvg_lookback,
            stop_buffer_atr=params.stop_buffer_atr,
        )

    if family_name == "inversion_fair_value_gap":
        from ..strategies.inversion_fair_value_gap import InversionFairValueGapStrategy
        return InversionFairValueGapStrategy(
            atr_period=params.atr_period,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
            min_gap_atr=params.min_gap_atr,
            fvg_lookback=params.fvg_lookback,
            inversion_lookback=params.inversion_lookback,
            retest_lookback=params.retest_lookback,
            stop_buffer_atr=params.stop_buffer_atr,
        )

    if family_name == "rsi_divergence":
        from ..strategies.rsi_divergence import RsiDivergenceStrategy
        return RsiDivergenceStrategy(
            rsi_period=params.rsi_period,
            atr_period=params.atr_period,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
            overbought=params.overbought,
            oversold=params.oversold,
            pivot_window=params.pivot_window,
            pivot_lookback=params.pivot_lookback,
            min_pivot_separation=params.min_pivot_separation,
            stop_buffer_atr=params.stop_buffer_atr,
        )

    if family_name == "ny_close_compression":
        from ..strategies.ny_close_compression import NYCloseCompressionStrategy
        return NYCloseCompressionStrategy(
            atr_period=params.atr_period,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
            min_breakout_atr=params.min_breakout_atr,
            min_range_atr=params.min_range_atr,
            max_range_atr=getattr(params, "max_range_atr", 0.50),
            min_range_bars=params.min_range_bars,
        )

    if family_name == "session_continuation":
        from ..strategies.session_continuation import SessionContinuationStrategy
        return SessionContinuationStrategy(
            atr_period=params.atr_period,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
            min_session_quantile=params.min_session_quantile,
            min_range_atr=params.min_range_atr,
            entry_slippage_buffer=params.entry_slippage_buffer,
        )

    if family_name == "dxy_lead_lag":
        from ..strategies.dxy_lead_lag import DXYLeadLagStrategy
        return DXYLeadLagStrategy(
            lookback=params.lookback,
            min_dxy_drop=params.min_dxy_drop,
            max_gold_response=params.max_gold_response,
            atr_period=params.atr_period,
            stop_atr_mult=params.stop_atr_mult,
            risk_reward=params.risk_reward,
            max_spread=params.max_spread,
            min_atr_threshold=getattr(params, "min_atr_threshold", 0.0),
        )

    if family_name == "real_yield_reversal":
        from ..strategies.real_yield_reversal import RealYieldReversalStrategy
        if macro is None:
            raise ValueError(
                "real_yield_reversal requires macro=MacroFrame; pass it via "
                "family_spec_with_macro(...) or make_strategy(..., macro=...)"
            )
        return RealYieldReversalStrategy(
            macro=macro,
            yield_lookback_days=params.yield_lookback_days,
            min_yield_move_bps=params.min_yield_move_bps,
            atr_period=params.atr_period,
            stop_atr_mult=params.stop_atr_mult,
            risk_reward=params.risk_reward,
            enter_longs=getattr(params, "enter_longs", True),
            enter_shorts=getattr(params, "enter_shorts", True),
            max_spread=getattr(params, "max_spread", 1.00),
            min_atr_threshold=getattr(params, "min_atr_threshold", 0.0),
        )

    if family_name == "timed_horizon_macro_regime":
        from ..strategies.timed_horizon_macro_regime import (
            TimedHorizonMacroRegimeStrategy,
        )
        if macro is None:
            raise ValueError(
                "timed_horizon_macro_regime requires macro=MacroFrame; pass it "
                "via family_spec_with_macro(...) or make_strategy(..., macro=...)"
            )
        return TimedHorizonMacroRegimeStrategy(
            macro=macro,
            real_yield_lookback_days=params.real_yield_lookback_days,
            real_yield_max_change_bps=params.real_yield_max_change_bps,
            vix_lookback_days=getattr(params, "vix_lookback_days", 5),
            vix_max_change_abs=params.vix_max_change_abs,
            require_dxy_flat=getattr(params, "require_dxy_flat", True),
            dxy_lookback_days=getattr(params, "dxy_lookback_days", 20),
            dxy_max_abs_change_pct=params.dxy_max_abs_change_pct,
            atr_period=getattr(params, "atr_period", 14),
            far_atr_mult=params.far_atr_mult,
            once_per_day=getattr(params, "once_per_day", True),
            require_bullish_close=getattr(params, "require_bullish_close", False),
            max_spread=getattr(params, "max_spread", 1.50),
        )

    raise ValueError(f"Unknown strategy family: {family_name!r}")
