"""Lower-timeframe entry-trigger refinement.

When a primary-timeframe strategy emits a setup at bar ``i``, the engine
normally fills at ``bar[i+1].open`` (with spread + slippage adjustments).
This module provides an alternative: scan a *lower*-timeframe series
(typically 5m or 1m) inside the window ``[primary[i+1].timestamp,
primary[i+1].timestamp + primary_tf)`` for a confirmation pattern; only
fill if confirmed, and use the LTF post-confirmation open as the entry
price.

This raises selectivity (drops setups that lose momentum during the
fill bar) and tightens entries (entry at structural level, not arbitrary
candle open).

Triggers
--------
- :class:`MomentumDisplacement` — LTF candle whose body exceeds N×ATR(L)
- :class:`Engulf`               — LTF bullish/bearish engulfing pattern
- :class:`StructureBreak`       — LTF break of the previous swing high/low

All triggers obey strict no-look-ahead: confirmation is observed on LTF
bar ``j``; the actual fill price is ``ltf_bars[j+1].open`` (i.e. the bar
*after* the confirmation closes).
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, Sequence

from ..models import MarketBar, Side, TradeSignal
from ..data.mtf import tf_duration


# ---------------------------------------------------------------------------
# Trigger protocol + implementations
# ---------------------------------------------------------------------------

class LTFTrigger(Protocol):
    name: str

    def confirm(
        self,
        side: Side,
        ltf_window: Sequence[MarketBar],
    ) -> int | None:
        """Return the LTF bar index inside ``ltf_window`` at which the
        confirmation pattern is *observed*, or ``None`` if not.

        The actual fill happens at the OPEN of bar ``index + 1`` to
        preserve no-look-ahead.  Therefore the implementation must
        return ``index < len(ltf_window) - 1``."""
        ...


@dataclass(frozen=True)
class MomentumDisplacement:
    """Body of bar > ``body_atr_mult`` × ATR(``atr_period``) and aligned
    with ``side`` direction."""
    name: str = "displacement"
    body_atr_mult: float = 0.6
    atr_period: int = 14

    def confirm(self, side: Side, ltf_window: Sequence[MarketBar]) -> int | None:
        if len(ltf_window) < self.atr_period + 2:
            return None
        # Build rolling ATR on the window itself (first atr_period are
        # warmup).  Iterate from atr_period+1 onwards so the bar HAS a
        # successor (so we can fill at j+1).
        trs: list[float] = []
        prev_close: float | None = None
        for b in ltf_window:
            trs.append(b.true_range(prev_close))
            prev_close = b.close
        # Simple moving ATR
        for j in range(self.atr_period, len(ltf_window) - 1):
            atr = sum(trs[j - self.atr_period + 1: j + 1]) / self.atr_period
            if atr <= 0:
                continue
            bar = ltf_window[j]
            body = bar.close - bar.open
            if side is Side.LONG and body > self.body_atr_mult * atr:
                return j
            if side is Side.SHORT and -body > self.body_atr_mult * atr:
                return j
        return None


@dataclass(frozen=True)
class Engulf:
    """Two-bar engulfing pattern in the direction of ``side``."""
    name: str = "engulf"

    def confirm(self, side: Side, ltf_window: Sequence[MarketBar]) -> int | None:
        for j in range(1, len(ltf_window) - 1):
            prev = ltf_window[j - 1]
            cur = ltf_window[j]
            if side is Side.LONG:
                if (cur.close > cur.open
                        and prev.close < prev.open
                        and cur.close >= prev.open
                        and cur.open <= prev.close):
                    return j
            else:
                if (cur.close < cur.open
                        and prev.close > prev.open
                        and cur.close <= prev.open
                        and cur.open >= prev.close):
                    return j
        return None


@dataclass(frozen=True)
class StructureBreak:
    """Close-through of the highest/lowest price seen earlier in the
    window (mini break-of-structure inside the fill bar)."""
    name: str = "structure_break"
    lookback: int = 3

    def confirm(self, side: Side, ltf_window: Sequence[MarketBar]) -> int | None:
        if len(ltf_window) < self.lookback + 2:
            return None
        for j in range(self.lookback, len(ltf_window) - 1):
            prior = ltf_window[j - self.lookback: j]
            cur = ltf_window[j]
            if side is Side.LONG:
                hi = max(b.high for b in prior)
                if cur.close > hi:
                    return j
            else:
                lo = min(b.low for b in prior)
                if cur.close < lo:
                    return j
        return None


# ---------------------------------------------------------------------------
# Resolver factory
# ---------------------------------------------------------------------------

@dataclass
class _LTFLookup:
    timestamps: list  # sorted timestamps
    bars: tuple[MarketBar, ...]

    def slice_window(self, lo, hi) -> tuple[MarketBar, ...]:
        a = bisect_left(self.timestamps, lo)
        b = bisect_right(self.timestamps, hi)
        return self.bars[a:b]

    def slice_with_context(self, lo, hi, context: int) -> tuple[tuple[MarketBar, ...], int]:
        """Return ``(slice, window_start_offset)`` where ``slice`` includes
        up to ``context`` bars BEFORE ``lo`` for indicator warmup, and
        ``window_start_offset`` is the index inside ``slice`` of the
        first bar at-or-after ``lo``."""
        a = bisect_left(self.timestamps, lo)
        b = bisect_right(self.timestamps, hi)
        ctx_start = max(0, a - context)
        return self.bars[ctx_start:b], a - ctx_start


def make_ltf_entry_resolver(
    ltf_bars: Sequence[MarketBar],
    primary_tf: str,
    trigger: LTFTrigger,
    *,
    apply_spread: bool = True,
    slippage_bps: float = 0.0,
    context_bars: int = 30,
):
    """Build an :class:`EntryPriceResolver` that:

    1. Locates LTF bars covering the next-primary-bar window
       ``[primary[i+1].ts, primary[i+1].ts + primary_tf)`` plus up to
       ``context_bars`` preceding bars (for trigger indicator warmup).
    2. Calls ``trigger.confirm(side, slice_with_context)``.
    3. Accepts the confirmation only if its timestamp lies INSIDE the
       primary window (the leading context is for warmup only).
    4. Returns the open price of the LTF bar AFTER confirmation, with
       spread and slippage applied just like the base engine.
    5. Returns ``None`` (drop signal) if no in-window confirmation.
    """
    pdur = tf_duration(primary_tf)
    lookup = _LTFLookup(
        timestamps=[b.timestamp for b in ltf_bars],
        bars=tuple(ltf_bars),
    )

    def _spread_adj(side: Side, price: float, spread: float) -> float:
        if not apply_spread:
            return price
        half = spread / 2.0
        return price + half if side is Side.LONG else price - half

    def _slip(side: Side, price: float) -> float:
        if slippage_bps <= 0.0:
            return price
        delta = price * slippage_bps / 10_000.0
        return price + delta if side is Side.LONG else price - delta

    def resolver(
        signal: TradeSignal,
        bars: Sequence[MarketBar],
        index: int,
        default_entry_price: float,
    ) -> float | None:
        if index + 1 >= len(bars):
            return None
        win_lo = bars[index + 1].timestamp
        win_hi = win_lo + pdur
        slice_, win_start = lookup.slice_with_context(
            win_lo, win_hi - timedelta(microseconds=1), context_bars,
        )
        if not slice_ or win_start >= len(slice_):
            return None
        idx = trigger.confirm(signal.side, slice_)
        if idx is None or idx + 1 >= len(slice_):
            return None
        # Confirmation must be inside the primary window (leading
        # context bars are warmup-only, not valid confirmation bars).
        if idx < win_start:
            return None
        fill_bar = slice_[idx + 1]
        # And the fill bar should also lie inside the window (otherwise
        # we'd fill in the next primary bar's window which is outside
        # the contract).  If fill_bar exceeds win_hi, drop.
        if fill_bar.timestamp >= win_hi:
            return None
        price = _spread_adj(signal.side, fill_bar.open, fill_bar.spread)
        price = _slip(signal.side, price)
        return price

    return resolver


__all__ = [
    "LTFTrigger",
    "MomentumDisplacement",
    "Engulf",
    "StructureBreak",
    "make_ltf_entry_resolver",
]
