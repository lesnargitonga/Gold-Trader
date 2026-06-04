"""RSI extremes + divergence + candlestick confirmation.

Concept
-------
At RSI overbought / oversold extremes, look for *regular* divergence between
price and RSI, then require a confirming reversal candle on the signal bar.
Stops are placed beyond the divergence swing extreme; targets are R-multiple
based, respecting structural anchors.

Logic
-----
1. Compute Wilder's RSI(period).
2. Identify pivot lows / pivot highs in price within `pivot_lookback` bars,
   using a centred window of size `pivot_window` (so the pivot is confirmed
   ``pivot_window`` bars after it forms — no lookahead).
3. Bullish setup (LONG):
   - Most-recent confirmed pivot-low B (price): bars[B].low < bars[A].low
     for an earlier confirmed pivot-low A within `pivot_lookback` bars.
   - RSI at B is GREATER than RSI at A (regular bullish divergence).
   - RSI on B was below `oversold` (default 30).
   - Signal bar (current ``index``) has bullish reversal candlestick:
     hammer OR bullish engulfing of the prior bar.
   - Stop = recent swing low (bars[B].low) − stop_buffer_atr × ATR.
   - Target = entry + RR × stop_dist.
4. Bearish setup (SHORT) mirrors with pivot highs and overbought/RSI lower.

Quality gates: spread <= max_spread, gap-aware lookback for ATR, no signal
without a confirmed pivot.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from . import filters as F
from . import scoring as S
from .base import lookback_spans_gap


# Three-tier filter system per HANDBOOK §11 redesign.
#
#   Tier 1 (universal vetos): news, weekend.
#   Tier 2 (strategy vetos):   min_swing — pivot must be a real swing.
#   Tier 3 (scored, 100 pts):  rsi_extreme (3-way 20/10/0),
#                              confirmation_lag (3-way 15/8/0),
#                              entry_session (binary 15),
#                              candle_quality (3-way 15/8/0),
#                              divergence_strength (3-way 15/8/0),
#                              htf_counter (3-way 10/5/0 — RSI div is
#                              reversal so HTF *against* trade dir gets
#                              full credit), pivot_separation (binary 10).
_DEFAULT_RSI_FILTERS = (
    "min_swing",              # Tier 2 — strategy veto
    "news",                   # Tier 1 — universal veto
    "weekend",                # Tier 1 — universal veto
    "rsi_extreme",            # Tier 3 scored binary (post-2026-05-10)
    "confirmation_lag",       # Tier 3 scored
    "entry_session",          # Tier 3 scored
    "candle_quality",         # Tier 3 scored
    "divergence_strength",    # Tier 3 scored
    "htf_counter",            # Tier 3 scored — sign flipped 2026-05-10
    "htf_against_penalty",    # Tier 3 scored penalty (post-2026-05-10)
    "pivot_separation",       # Tier 3 scored
)


@dataclass(frozen=True)
class RsiDivergenceStrategy:
    rsi_period: int = 14
    atr_period: int = 14
    risk_reward: float = 2.0
    max_spread: float = 1.00
    overbought: float = 70.0
    oversold: float = 30.0
    pivot_window: int = 3            # centred window: bar is pivot if extreme over ±N
    pivot_lookback: int = 40         # how far back to scan for a prior pivot
    min_pivot_separation: int = 4    # bars between A and B
    stop_buffer_atr: float = 0.15
    entry_slippage_buffer: float = 0.1
    # Discretionary checklist filters (HANDBOOK §11 Option E).
    filters_enabled: tuple[str, ...] = _DEFAULT_RSI_FILTERS
    swing_lookback: int = 20
    swing_min_atr: float = 1.0
    rsi_extreme_long: float = 35.0   # bullish-div: RSI(B) must be <= this
    rsi_extreme_short: float = 65.0  # bearish-div: RSI(B) must be >= this
    confirmation_max_lag: int = 5    # incl. unavoidable pivot_window confirmation latency
    confirmation_partial_lag: int = 3  # 3-way: <= partial 15, <= max 8, else 0
    rsi_partial_long: float = 40.0   # 3-way: <=extreme 20, <=partial 10, else 0
    rsi_partial_short: float = 60.0
    divergence_strength_min: float = 5.0   # rsi_diff between A and B for partial credit
    divergence_strength_full: float = 10.0
    pivot_separation_full: int = 8         # bars between A & B for binary 10 pts
    htf_minutes: int = 240
    htf_ema_fast: int = 20
    htf_ema_slow: int = 50
    score_full_threshold: int = 70
    score_half_threshold: int = 55
    score_log_threshold: int = 40
    name: str = "rsi_divergence"

    def warmup_bars(self) -> int:
        return self.rsi_period + self.pivot_lookback + self.pivot_window + 2

    # ------------------------------------------------------------------
    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        bar = bars[index]
        if bar.spread > self.max_spread:
            return None
        if index < self.warmup_bars():
            return None
        if lookback_spans_gap(bars, index, self.pivot_lookback):
            return None

        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None
        slippage = self.entry_slippage_buffer * atr
        stop_buf = self.stop_buffer_atr * atr

        rsi_series = self._rsi_series(bars, index)
        if not rsi_series:
            return None
        # rsi_series is aligned with bars[0..index]; rsi_series[i] corresponds to bars[i].

        prev = bars[index - 1]

        # ------------------ Bullish divergence -> LONG ------------------
        b_idx = self._latest_pivot_low(bars, index)
        if b_idx is not None:
            a_idx = self._earlier_pivot_low(bars, b_idx)
            if (
                a_idx is not None
                and (b_idx - a_idx) >= self.min_pivot_separation
                and bars[b_idx].low < bars[a_idx].low                 # price LL
                and rsi_series[b_idx] > rsi_series[a_idx]              # RSI HL
                and rsi_series[b_idx] < self.oversold                  # extreme
            ):
                ok_candle = self._is_bullish_reversal_candle(prev, bar)
                score = self._score_signal(
                    bars, index, side=Side.LONG, pivot_idx=b_idx,
                    earlier_pivot_idx=a_idx,
                    rsi_at_b=rsi_series[b_idx], rsi_at_a=rsi_series[a_idx],
                    candle_ok=ok_candle,
                )
                # Without scoring (filters_enabled=()), preserve legacy
                # behaviour: candle confirmation is a hard gate.
                if score is None and not ok_candle:
                    pass  # fall through to short branch
                elif score is not None and score.verdict is S.ScoreVerdict.REJECT:
                    object.__setattr__(self, "_last_filter_rejection",
                                       f"reject(score={score.score:.0f},veto={score.vetoed_by})")
                else:
                    assumed_entry = bar.close + slippage
                    stop = bars[b_idx].low - stop_buf
                    stop_dist = assumed_entry - stop
                    if stop_dist > 0:
                        size_mult = score.size_multiplier if score is not None else 1.0
                        score_val = score.score if score is not None else 0.0
                        return TradeSignal(
                            side=Side.LONG,
                            stop=stop,
                            target=assumed_entry + stop_dist * self.risk_reward,
                            reason=(
                                f"RSI bull divergence: pivot lows {a_idx}->{b_idx}, "
                                f"RSI {rsi_series[a_idx]:.1f}->{rsi_series[b_idx]:.1f}, "
                                f"reversal candle confirmed, atr={atr:.2f}, "
                                f"score={score_val:.0f}."
                            ),
                            tags=("rsi_div", "bullish", "long"),
                            risk_reward=self.risk_reward,
                            size_multiplier=size_mult,
                            score=score_val,
                        )

        # ------------------ Bearish divergence -> SHORT -----------------
        b_idx = self._latest_pivot_high(bars, index)
        if b_idx is not None:
            a_idx = self._earlier_pivot_high(bars, b_idx)
            if (
                a_idx is not None
                and (b_idx - a_idx) >= self.min_pivot_separation
                and bars[b_idx].high > bars[a_idx].high                # price HH
                and rsi_series[b_idx] < rsi_series[a_idx]              # RSI LH
                and rsi_series[b_idx] > self.overbought                # extreme
            ):
                ok_candle = self._is_bearish_reversal_candle(prev, bar)
                score = self._score_signal(
                    bars, index, side=Side.SHORT, pivot_idx=b_idx,
                    earlier_pivot_idx=a_idx,
                    rsi_at_b=rsi_series[b_idx], rsi_at_a=rsi_series[a_idx],
                    candle_ok=ok_candle,
                )
                if score is None and not ok_candle:
                    pass
                elif score is not None and score.verdict is S.ScoreVerdict.REJECT:
                    object.__setattr__(self, "_last_filter_rejection",
                                       f"reject(score={score.score:.0f},veto={score.vetoed_by})")
                else:
                    assumed_entry = bar.close - slippage
                    stop = bars[b_idx].high + stop_buf
                    stop_dist = stop - assumed_entry
                    if stop_dist > 0:
                        size_mult = score.size_multiplier if score is not None else 1.0
                        score_val = score.score if score is not None else 0.0
                        return TradeSignal(
                            side=Side.SHORT,
                            stop=stop,
                            target=assumed_entry - stop_dist * self.risk_reward,
                            reason=(
                                f"RSI bear divergence: pivot highs {a_idx}->{b_idx}, "
                                f"RSI {rsi_series[a_idx]:.1f}->{rsi_series[b_idx]:.1f}, "
                                f"reversal candle confirmed, atr={atr:.2f}, "
                                f"score={score_val:.0f}."
                            ),
                            tags=("rsi_div", "bearish", "short"),
                            risk_reward=self.risk_reward,
                            size_multiplier=size_mult,
                            score=score_val,
                        )

        return None

    # ------------------------------------------------------------------
    # Indicator helpers
    # ------------------------------------------------------------------
    def _rsi_series(self, bars: Sequence[MarketBar], index: int) -> list[float]:
        """Wilder's RSI cached per-(bars, period).

        First call computes the full RSI sweep over all of ``bars`` in O(N).
        Subsequent calls during the same backtest return a slice — eliminates
        the O(N^2) rebuild that was the main hot loop.
        """
        cache_key = (id(bars), len(bars), self.rsi_period)
        cache = getattr(self, "_rsi_cache", None)
        if cache is None or cache.get("key") != cache_key:
            cache = {"key": cache_key, "rsi": self._compute_rsi_full(bars)}
            object.__setattr__(self, "_rsi_cache", cache)
        rsi: list[float] = cache["rsi"]
        if index < self.rsi_period:
            return []
        return rsi

    def _compute_rsi_full(self, bars: Sequence[MarketBar]) -> list[float]:
        n = self.rsi_period
        N = len(bars)
        rsi = [50.0] * N
        if N <= n:
            return rsi
        # Wilder smoothing — single pass, O(N).
        gain_sum = 0.0
        loss_sum = 0.0
        for i in range(1, n + 1):
            d = bars[i].close - bars[i - 1].close
            if d > 0: gain_sum += d
            else: loss_sum -= d
        avg_gain = gain_sum / n
        avg_loss = loss_sum / n
        rsi[n] = 100.0 if avg_loss <= 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
        inv_n = 1.0 / n
        coef = (n - 1) * inv_n
        for i in range(n + 1, N):
            d = bars[i].close - bars[i - 1].close
            g = d if d > 0 else 0.0
            l = -d if d < 0 else 0.0
            avg_gain = avg_gain * coef + g * inv_n
            avg_loss = avg_loss * coef + l * inv_n
            rsi[i] = 100.0 if avg_loss <= 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
        return rsi

    def _atr(self, bars: Sequence[MarketBar], index: int) -> float:
        cache_key = (id(bars), len(bars), self.atr_period)
        cache = getattr(self, "_atr_cache", None)
        if cache is None or cache.get("key") != cache_key:
            cache = {"key": cache_key, "atr": self._compute_atr_full(bars)}
            object.__setattr__(self, "_atr_cache", cache)
        atr: list[float] = cache["atr"]
        if index < self.atr_period:
            return 0.0
        return atr[index]

    def _compute_atr_full(self, bars: Sequence[MarketBar]) -> list[float]:
        N = len(bars)
        p = self.atr_period
        atr = [0.0] * N
        if N <= p:
            return atr
        # Original semantics: simple mean of TR over the last `p` bars.
        # Implemented as a rolling sum, O(N).
        prev_close = bars[0].close
        tr: list[float] = [0.0]  # tr[0] unused; index 1.. valid
        for i in range(1, N):
            tr.append(bars[i].true_range(prev_close))
            prev_close = bars[i].close
        window_sum = sum(tr[1:p + 1])
        atr[p] = window_sum / p
        for i in range(p + 1, N):
            window_sum += tr[i] - tr[i - p]
            atr[i] = window_sum / p
        return atr

    # ------------------------------------------------------------------
    # Pivot detection (no lookahead — pivot at ``i`` is only confirmed once
    # ``pivot_window`` bars have passed)
    # ------------------------------------------------------------------
    def _latest_pivot_low(self, bars: Sequence[MarketBar], index: int) -> int | None:
        w = self.pivot_window
        # Last confirmable pivot index is ``index - w``.
        latest = index - w
        earliest = max(w, index - self.pivot_lookback)
        for i in range(latest, earliest - 1, -1):
            if self._is_pivot_low(bars, i, w):
                return i
        return None

    def _earlier_pivot_low(self, bars: Sequence[MarketBar], b_idx: int) -> int | None:
        w = self.pivot_window
        earliest = max(w, b_idx - self.pivot_lookback)
        for i in range(b_idx - self.min_pivot_separation, earliest - 1, -1):
            if self._is_pivot_low(bars, i, w):
                return i
        return None

    def _latest_pivot_high(self, bars: Sequence[MarketBar], index: int) -> int | None:
        w = self.pivot_window
        latest = index - w
        earliest = max(w, index - self.pivot_lookback)
        for i in range(latest, earliest - 1, -1):
            if self._is_pivot_high(bars, i, w):
                return i
        return None

    def _earlier_pivot_high(self, bars: Sequence[MarketBar], b_idx: int) -> int | None:
        w = self.pivot_window
        earliest = max(w, b_idx - self.pivot_lookback)
        for i in range(b_idx - self.min_pivot_separation, earliest - 1, -1):
            if self._is_pivot_high(bars, i, w):
                return i
        return None

    @staticmethod
    def _is_pivot_low(bars: Sequence[MarketBar], i: int, w: int) -> bool:
        lo = bars[i].low
        for j in range(i - w, i + w + 1):
            if j == i:
                continue
            if j < 0 or j >= len(bars):
                return False
            if bars[j].low <= lo:
                return False
        return True

    @staticmethod
    def _is_pivot_high(bars: Sequence[MarketBar], i: int, w: int) -> bool:
        hi = bars[i].high
        for j in range(i - w, i + w + 1):
            if j == i:
                continue
            if j < 0 or j >= len(bars):
                return False
            if bars[j].high >= hi:
                return False
        return True

    # ------------------------------------------------------------------
    # Candlestick patterns (signal bar = ``bar``, prior = ``prev``)
    # ------------------------------------------------------------------
    @staticmethod
    def _is_bullish_reversal_candle(prev: MarketBar, bar: MarketBar) -> bool:
        rng = bar.high - bar.low
        if rng <= 0:
            return False
        body = abs(bar.close - bar.open)
        lower_wick = min(bar.open, bar.close) - bar.low
        upper_wick = bar.high - max(bar.open, bar.close)
        # Hammer: small body in upper third, long lower wick (>=2x body)
        is_hammer = (
            body > 0
            and lower_wick >= 2.0 * body
            and upper_wick <= body
            and bar.close > bar.open
        )
        # Bullish engulfing: prev bearish, current bullish, current body engulfs prev body
        is_engulf = (
            prev.close < prev.open
            and bar.close > bar.open
            and bar.open <= prev.close
            and bar.close >= prev.open
            and body > abs(prev.close - prev.open)
        )
        return is_hammer or is_engulf

    @staticmethod
    def _is_bearish_reversal_candle(prev: MarketBar, bar: MarketBar) -> bool:
        rng = bar.high - bar.low
        if rng <= 0:
            return False
        body = abs(bar.close - bar.open)
        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick = min(bar.open, bar.close) - bar.low
        # Shooting star: small body in lower third, long upper wick
        is_star = (
            body > 0
            and upper_wick >= 2.0 * body
            and lower_wick <= body
            and bar.close < bar.open
        )
        # Bearish engulfing
        is_engulf = (
            prev.close > prev.open
            and bar.close < bar.open
            and bar.open >= prev.close
            and bar.close <= prev.open
            and body > abs(prev.close - prev.open)
        )
        return is_star or is_engulf

    # ------------------------------------------------------------------
    # Three-tier filter scoring (HANDBOOK §11 redesign)
    # ------------------------------------------------------------------
    def _score_signal(
        self,
        bars: Sequence[MarketBar],
        index: int,
        *,
        side: Side,
        pivot_idx: int,
        earlier_pivot_idx: int,
        rsi_at_b: float,
        rsi_at_a: float,
        candle_ok: bool,
    ) -> S.SignalScore | None:
        if not self.filters_enabled:
            return None
        atr = self._atr(bars, index)
        bar = bars[index]
        results: list[S.FilterResult] = []
        for f in self.filters_enabled:
            if f == "min_swing":
                results.append(S.veto(
                    "min_swing", S.FilterTier.STRATEGY_VETO,
                    lambda: F.min_swing_size(
                        bars, pivot_idx, side, atr,
                        min_atr=self.swing_min_atr, lookback=self.swing_lookback,
                    ),
                ))
            elif f == "news":
                results.append(S.veto(
                    "news", S.FilterTier.UNIVERSAL_VETO,
                    lambda: F.news_clear(bar),
                ))
            elif f == "weekend":
                results.append(S.veto(
                    "weekend", S.FilterTier.UNIVERSAL_VETO,
                    lambda: F.weekend_clear(bar),
                ))
            elif f == "rsi_extreme":
                # BINARY (post-2026-05-10 calibration): the 40/60
                # partial-credit band gave points to RSI readings that
                # are merely "off-centre", not extreme.  RSI=40 in a
                # parabolic gold market is a breather, not oversold.
                # Now: full points only when actually past the strict
                # extreme threshold; everything else is zero.
                def _rsi_classify():
                    if side is Side.LONG:
                        passed = rsi_at_b <= self.oversold
                        return (passed, f"rsi(B)={rsi_at_b:.1f} vs oversold={self.oversold}")
                    passed = rsi_at_b >= self.overbought
                    return (passed, f"rsi(B)={rsi_at_b:.1f} vs overbought={self.overbought}")
                results.append(S.scored_binary("rsi_extreme", 20, _rsi_classify))
            elif f == "confirmation_lag":
                def _lag_classify():
                    delta = index - pivot_idx
                    if delta < 0:
                        return ("none", f"lag={delta}")
                    if delta <= self.confirmation_partial_lag:
                        return ("full", f"lag={delta}")
                    if delta <= self.confirmation_max_lag:
                        return ("partial", f"lag={delta}")
                    return ("none", f"lag={delta}>{self.confirmation_max_lag}")
                results.append(S.scored_three_way("confirmation_lag", 15, 8, _lag_classify))
            elif f == "entry_session":
                results.append(S.scored_binary(
                    "entry_session", 15,
                    lambda: F.in_session(bar, allowed=("london", "ny")),
                ))
            elif f == "candle_quality":
                # Reversal candle: full if hammer/shooting-star OR engulfing.
                # Partial if any reversal-shaped close (close beats open in
                # the trade direction with body >= 30% of range).
                def _candle_classify():
                    if candle_ok:
                        return ("full", "reversal_candle")
                    rng = bar.high - bar.low
                    if rng <= 0:
                        return ("none", "no_range")
                    body = abs(bar.close - bar.open)
                    body_pct = body / rng
                    if side is Side.LONG and bar.close > bar.open and body_pct >= 0.30:
                        return ("partial", f"weak_bull(body={body_pct:.2f})")
                    if side is Side.SHORT and bar.close < bar.open and body_pct >= 0.30:
                        return ("partial", f"weak_bear(body={body_pct:.2f})")
                    return ("none", f"no_reversal(body={body_pct:.2f})")
                results.append(S.scored_three_way("candle_quality", 15, 8, _candle_classify))
            elif f == "divergence_strength":
                def _div_classify():
                    diff = abs(rsi_at_b - rsi_at_a)
                    if diff >= self.divergence_strength_full:
                        return ("full", f"rsi_diff={diff:.1f}")
                    if diff >= self.divergence_strength_min:
                        return ("partial", f"rsi_diff={diff:.1f}")
                    return ("none", f"rsi_diff={diff:.1f}<{self.divergence_strength_min}")
                results.append(S.scored_three_way("divergence_strength", 15, 8, _div_classify))
            elif f == "htf_counter":
                # SIGN FLIPPED 2026-05-10 (very late):
                # Empirical: 5y/15m score-vs-PF showed RSI scoring
                # *inversely* monotonic — high score = worse PF.  The
                # textbook "reversal against HTF = max confluence" was
                # the cause: trading bullish divergence into bearish
                # HTF on dominantly-bullish XAUUSD systematically fails.
                # Renamed semantically — full points when HTF ALIGNED
                # with trade direction (continuation-divergence), zero
                # when opposing.  Plus a separate penalty filter
                # ``htf_against_penalty`` for active misalignment.
                def _htf_classify():
                    ef, es, idx_map = F.htf_ema_aligned(
                        bars, self.htf_minutes, self.htf_ema_fast, self.htf_ema_slow,
                    )
                    h = idx_map[index]
                    if h < self.htf_ema_slow:
                        return (False, f"htf_warmup(h={h})")
                    diff = ef[h] - es[h]
                    if abs(diff) < 0.10 * atr:
                        return (False, f"htf_flat(diff={diff:.2f})")
                    aligned = (side is Side.LONG and diff > 0) or (side is Side.SHORT and diff < 0)
                    return (aligned, f"htf_diff={diff:.2f}")
                results.append(S.scored_binary("htf_counter", 10, _htf_classify))
            elif f == "htf_against_penalty":
                # NEW: −15 when HTF is actively against trade direction.
                # Pairs with the (now-aligned) htf_counter so trading
                # *with* HTF nets +10, *against* HTF nets −15.
                def _htf_against():
                    ef, es, idx_map = F.htf_ema_aligned(
                        bars, self.htf_minutes, self.htf_ema_fast, self.htf_ema_slow,
                    )
                    h = idx_map[index]
                    if h < self.htf_ema_slow:
                        return (False, "htf_warmup")
                    diff = ef[h] - es[h]
                    if abs(diff) < 0.10 * atr:
                        return (False, f"htf_flat(diff={diff:.2f})")
                    against = (side is Side.LONG and diff < 0) or (side is Side.SHORT and diff > 0)
                    return (against, f"htf_against(diff={diff:.2f})")
                results.append(S.scored_penalty("htf_against_penalty", 15, _htf_against))
            elif f == "pivot_separation":
                def _sep():
                    sep = pivot_idx - earlier_pivot_idx
                    if sep >= self.pivot_separation_full:
                        return True, f"sep={sep}"
                    return False, f"sep={sep}<{self.pivot_separation_full}"
                results.append(S.scored_binary("pivot_separation", 10, _sep))
            elif f == "htf_trend":
                # legacy 3-way HTF aligned (mirror of IFVG)
                def _htf_aligned():
                    ef, es, idx_map = F.htf_ema_aligned(
                        bars, self.htf_minutes, self.htf_ema_fast, self.htf_ema_slow,
                    )
                    h = idx_map[index]
                    if h < self.htf_ema_slow:
                        return ("partial", f"htf_warmup(h={h})")
                    diff = ef[h] - es[h]
                    if abs(diff) < 0.05 * atr:
                        return ("partial", f"htf_neutral(diff={diff:.2f})")
                    if (side is Side.LONG and diff > 0) or (side is Side.SHORT and diff < 0):
                        return ("full", f"htf_aligned(diff={diff:.2f})")
                    return ("none", f"htf_opposing(diff={diff:.2f})")
                results.append(S.scored_three_way("htf_trend", 10, 5, _htf_aligned))
        return S.aggregate_results(
            results,
            full_threshold=self.score_full_threshold,
            half_threshold=self.score_half_threshold,
            log_threshold=self.score_log_threshold,
        )
