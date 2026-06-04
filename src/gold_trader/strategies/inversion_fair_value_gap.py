"""Inversion Fair Value Gap (IFVG / inverted FVG) strategy.

Concept
-------
A standard 3-bar Fair Value Gap is an imbalance where price moved aggressively
through a region without filling it.  When price later trades back **through
the entire gap and closes beyond the far edge**, the gap is said to *invert*:
the zone that was previously support flips to resistance (and vice-versa).
The first retest of the inverted gap is a high-probability mean-reversion
entry.

Logic
-----
Detect a 3-bar FVG at bar k:
  • Bullish FVG → fvg = (bars[k-2].high, bars[k].low).  When a later bar j
    closes BELOW bars[k-2].high the gap inverts → upper edge becomes
    resistance.  On a subsequent retest from below where the bar's high
    pierces the inverted zone but the close stays below it → SHORT.
    Stop = inverted-zone top + buffer.
  • Bearish FVG mirrors → LONG when retest from above closes back above
    the inverted floor.

Quality gates
-------------
- Gap formation impulse bar (k-1) must be in London/NY (UTC 7..21).
- 3-bar pattern must not span a >4h gap (weekend/daily close).
- Inversion must occur within `inversion_lookback` bars of the gap.
- Retest must happen within `retest_lookback` bars after inversion.
- Gap size >= `min_gap_atr` × ATR14 to filter noise.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Sequence

from ..models import MarketBar, Side, TradeSignal
from . import filters as F
from . import scoring as S
from .base import lookback_spans_gap

_MAX_FVG_SPAN = timedelta(hours=4)


# Three-tier filter system per HANDBOOK §11 redesign.
#
#   Tier 1 (universal vetos): news, weekend.  Spread is already gated
#                              upstream by ``max_spread`` field.
#   Tier 2 (strategy vetos):   prior_sweep — without a liquidity sweep
#                              there's no IFVG context to score.
#   Tier 3 (scored, 100 pts):  htf_trend (20/8/0), formation_session
#                              (15), displacement (15/8/0), gap_recency
#                              (10/6/0), gap_unmitigated (15/8/0),
#                              retest_quality (15), gap_size (10).
#
# Strategies declare ``filters_enabled``.  Empty tuple = scoring off
# (full size, used by unit tests with synthetic data).
_DEFAULT_IFVG_FILTERS = (
    "prior_sweep",            # Tier 2 — strategy veto
    "news",                   # Tier 1 — universal veto
    "weekend",                # Tier 1 — universal veto
    "htf_trend",              # Tier 3 scored binary (post-2026-05-10)
    "htf_counter_penalty",    # Tier 3 scored penalty (post-2026-05-10)
    "formation_session",      # Tier 3 scored
    "displacement",           # Tier 3 scored
    "gap_recency",            # Tier 3 scored
    "gap_unmitigated",        # Tier 3 scored
    "retest_quality",         # Tier 3 scored
    "gap_size",               # Tier 3 scored
)


@dataclass(frozen=True)
class InversionFairValueGapStrategy:
    atr_period: int = 14
    risk_reward: float = 2.0
    max_spread: float = 1.00
    min_gap_atr: float = 0.10
    fvg_lookback: int = 30           # bars to scan back for FVG formations
    inversion_lookback: int = 20     # bars after FVG within which inversion must occur
    retest_lookback: int = 10        # bars after inversion within which retest must occur
    stop_buffer_atr: float = 0.15
    entry_slippage_buffer: float = 0.1
    # Discretionary checklist filters (HANDBOOK §11 Option E).
    filters_enabled: tuple[str, ...] = _DEFAULT_IFVG_FILTERS
    sweep_lookback: int = 20
    htf_minutes: int = 240
    htf_ema_fast: int = 20
    htf_ema_slow: int = 50
    impulse_min_atr: float = 1.5
    impulse_partial_atr: float = 1.0  # 3-way scored: >=full=15, >=partial=8, else 0
    gap_max_age_bars: int = 12
    gap_partial_age_bars: int = 6     # 3-way: <=partial 10, <=max 6, else 0
    gap_max_fill_pct: float = 0.30
    gap_partial_fill_pct: float = 0.15  # 3-way: <=partial 15, <=max 8, else 0
    gap_size_atr: float = 0.50          # binary 10pts: gap_height >= this * ATR
    # Score thresholds (calibrate from holdout score distribution)
    score_full_threshold: int = 70
    score_half_threshold: int = 55
    score_log_threshold: int = 40
    name: str = "inversion_fair_value_gap"

    def warmup_bars(self) -> int:
        return self.atr_period + self.fvg_lookback + self.inversion_lookback + 3

    # ------------------------------------------------------------------
    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        bar = bars[index]
        if bar.spread > self.max_spread:
            return None
        if lookback_spans_gap(bars, index, self.atr_period):
            return None
        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None

        min_gap = self.min_gap_atr * atr
        slippage = self.entry_slippage_buffer * atr
        stop_buf = self.stop_buffer_atr * atr

        scan_start = max(2, index - self.fvg_lookback - self.inversion_lookback)
        # Walk newest -> oldest so we trigger on the freshest IFVG retest.
        for k in range(index - 1, scan_start - 1, -1):
            if k - 2 < 0:
                break

            b_prev2 = bars[k - 2]
            b_curr = bars[k]

            # Quality: impulse bar must be London/NY
            impulse_utc = bars[k - 1].timestamp.astimezone(timezone.utc)
            if not (7 <= impulse_utc.hour < 21):
                continue
            # No weekend / session-break spans inside the 3-bar pattern
            if b_curr.timestamp - b_prev2.timestamp > _MAX_FVG_SPAN:
                continue

            # ---------- Bullish FVG candidate (inverts to resistance) ----------
            if b_prev2.high < b_curr.low:
                fvg_bot = b_prev2.high
                fvg_top = b_curr.low
                if fvg_top - fvg_bot < min_gap:
                    continue

                inv_idx = self._find_inversion(
                    bars, k, index, level=fvg_bot, direction="below",
                    max_steps=self.inversion_lookback,
                )
                if inv_idx is None:
                    continue
                # Only consider the most recent retest opportunity
                if index - inv_idx > self.retest_lookback:
                    continue

                # Inverted zone now acts as resistance: (fvg_bot, fvg_top)
                # Retest condition: bar pierces zone from below and closes back below
                # the lower edge (rejection).
                if bar.high >= fvg_bot and bar.close <= fvg_bot:
                    score = self._score_signal(
                        bars, index, side=Side.SHORT,
                        formation_idx=k, impulse_idx=k - 1,
                        gap_bot=fvg_bot, gap_top=fvg_top,
                    )
                    if score is not None and score.verdict is S.ScoreVerdict.REJECT:
                        object.__setattr__(self, "_last_filter_rejection",
                                           f"reject(score={score.score:.0f},veto={score.vetoed_by})")
                        continue
                    assumed_entry = bar.close - slippage
                    stop = fvg_top + stop_buf
                    stop_dist = stop - assumed_entry
                    if stop_dist > 0:
                        size_mult = score.size_multiplier if score is not None else 1.0
                        score_val = score.score if score is not None else 0.0
                        return TradeSignal(
                            side=Side.SHORT,
                            stop=stop,
                            target=assumed_entry - stop_dist * self.risk_reward,
                            reason=(
                                f"IFVG short: inverted bullish gap "
                                f"({fvg_bot:.2f}-{fvg_top:.2f}) rejected, atr={atr:.2f}, "
                                f"score={score_val:.0f}."
                            ),
                            tags=("ifvg", "retest", "short"),
                            risk_reward=self.risk_reward,
                            size_multiplier=size_mult,
                            score=score_val,
                        )

            # ---------- Bearish FVG candidate (inverts to support) ----------
            elif b_prev2.low > b_curr.high:
                fvg_bot = b_curr.high
                fvg_top = b_prev2.low
                if fvg_top - fvg_bot < min_gap:
                    continue

                inv_idx = self._find_inversion(
                    bars, k, index, level=fvg_top, direction="above",
                    max_steps=self.inversion_lookback,
                )
                if inv_idx is None:
                    continue
                if index - inv_idx > self.retest_lookback:
                    continue

                # Inverted zone is now support; retest from above.
                if bar.low <= fvg_top and bar.close >= fvg_top:
                    score = self._score_signal(
                        bars, index, side=Side.LONG,
                        formation_idx=k, impulse_idx=k - 1,
                        gap_bot=fvg_bot, gap_top=fvg_top,
                    )
                    if score is not None and score.verdict is S.ScoreVerdict.REJECT:
                        object.__setattr__(self, "_last_filter_rejection",
                                           f"reject(score={score.score:.0f},veto={score.vetoed_by})")
                        continue
                    assumed_entry = bar.close + slippage
                    stop = fvg_bot - stop_buf
                    stop_dist = assumed_entry - stop
                    if stop_dist > 0:
                        size_mult = score.size_multiplier if score is not None else 1.0
                        score_val = score.score if score is not None else 0.0
                        return TradeSignal(
                            side=Side.LONG,
                            stop=stop,
                            target=assumed_entry + stop_dist * self.risk_reward,
                            reason=(
                                f"IFVG long: inverted bearish gap "
                                f"({fvg_bot:.2f}-{fvg_top:.2f}) reclaimed, atr={atr:.2f}, "
                                f"score={score_val:.0f}."
                            ),
                            tags=("ifvg", "retest", "long"),
                            risk_reward=self.risk_reward,
                            size_multiplier=size_mult,
                            score=score_val,
                        )

        return None

    # ------------------------------------------------------------------
    def _find_inversion(
        self,
        bars: Sequence[MarketBar],
        formation_idx: int,
        current_idx: int,
        *,
        level: float,
        direction: str,
        max_steps: int,
    ) -> int | None:
        """Return index of first bar that closes through ``level`` after the
        FVG formation. ``direction='below'`` inverts a bullish gap (close < level);
        ``direction='above'`` inverts a bearish gap (close > level)."""
        end = min(current_idx, formation_idx + max_steps)
        for j in range(formation_idx + 1, end + 1):
            if direction == "below" and bars[j].close < level:
                return j
            if direction == "above" and bars[j].close > level:
                return j
        return None

    def _atr(self, bars: Sequence[MarketBar], index: int) -> float:
        start = index - self.atr_period + 1
        atr_bars = bars[start : index + 1]
        previous_close = bars[start - 1].close if start > 0 else None
        true_ranges: list[float] = []
        for b in atr_bars:
            true_ranges.append(b.true_range(previous_close))
            previous_close = b.close
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0

    # ------------------------------------------------------------------
    # Three-tier filter scoring (HANDBOOK §11 redesign)
    # ------------------------------------------------------------------
    def _score_signal(
        self,
        bars: Sequence[MarketBar],
        index: int,
        *,
        side: Side,
        formation_idx: int,
        impulse_idx: int,
        gap_bot: float,
        gap_top: float,
    ) -> S.SignalScore | None:
        """Run the enabled filters and aggregate into a SignalScore.

        Returns ``None`` if ``filters_enabled`` is empty (scoring off,
        used by unit tests with synthetic bars).
        """
        if not self.filters_enabled:
            return None

        atr = self._atr(bars, index)
        bar = bars[index]
        results: list[S.FilterResult] = []

        for f in self.filters_enabled:
            if f == "prior_sweep":
                results.append(S.veto(
                    "prior_sweep", S.FilterTier.STRATEGY_VETO,
                    lambda: F.prior_swing_swept(bars, index, side, lookback=self.sweep_lookback),
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
            elif f == "htf_trend":
                # BINARY (post-2026-05-10 calibration): partial-credit
                # "neutral" bucket created the [60,70) PF=0.51 trough on
                # 5y/15m — flat HTF is not "partial confluence", it's a
                # trap.  Now: full points when HTF strongly aligned,
                # zero otherwise.  Plus a separate penalty filter
                # ``htf_counter_penalty`` that subtracts points when
                # HTF is actively opposing the trade direction.
                def _htf_aligned():
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
                results.append(S.scored_binary("htf_trend", 20, _htf_aligned))
            elif f == "htf_counter_penalty":
                # NEW: penalize opposing HTF aggressively (−20).  Pairs
                # with the binary htf_trend filter so the spread between
                # "HTF aligned" and "HTF opposing" is now 40 points
                # instead of 20.
                def _htf_opposing():
                    ef, es, idx_map = F.htf_ema_aligned(
                        bars, self.htf_minutes, self.htf_ema_fast, self.htf_ema_slow,
                    )
                    h = idx_map[index]
                    if h < self.htf_ema_slow:
                        return (False, "htf_warmup")
                    diff = ef[h] - es[h]
                    if abs(diff) < 0.10 * atr:
                        return (False, f"htf_flat(diff={diff:.2f})")
                    opposing = (side is Side.LONG and diff < 0) or (side is Side.SHORT and diff > 0)
                    return (opposing, f"htf_opposing(diff={diff:.2f})")
                results.append(S.scored_penalty("htf_counter_penalty", 20, _htf_opposing))
            elif f == "formation_session":
                results.append(S.scored_binary(
                    "formation_session", 15,
                    lambda: F.in_session(bars[impulse_idx], allowed=("london", "ny")),
                ))
            elif f == "displacement":
                def _disp_classify():
                    impulse = bars[impulse_idx]
                    body = abs(impulse.close - impulse.open)
                    if atr <= 0:
                        return ("none", "atr_zero")
                    ratio = body / atr
                    if ratio >= self.impulse_min_atr:
                        return ("full", f"body={ratio:.2f}*atr")
                    if ratio >= self.impulse_partial_atr:
                        return ("partial", f"body={ratio:.2f}*atr")
                    return ("none", f"body={ratio:.2f}*atr<{self.impulse_partial_atr}")
                results.append(S.scored_three_way("displacement", 15, 8, _disp_classify))
            elif f == "gap_recency":
                def _age_classify():
                    age = index - formation_idx
                    if age <= self.gap_partial_age_bars:
                        return ("full", f"age={age}")
                    if age <= self.gap_max_age_bars:
                        return ("partial", f"age={age}")
                    return ("none", f"age={age}>{self.gap_max_age_bars}")
                results.append(S.scored_three_way("gap_recency", 10, 6, _age_classify))
            elif f == "gap_unmitigated":
                def _fill_classify():
                    width = gap_top - gap_bot
                    if width <= 0:
                        return ("none", "zero_width")
                    deepest = 0.0
                    for i in range(formation_idx + 1, index):
                        b = bars[i]
                        pen = max(0.0, min(gap_top, b.high) - max(gap_bot, b.low))
                        if pen > deepest:
                            deepest = pen
                    pct = deepest / width
                    if pct <= self.gap_partial_fill_pct:
                        return ("full", f"fill={pct:.2f}")
                    if pct <= self.gap_max_fill_pct:
                        return ("partial", f"fill={pct:.2f}")
                    return ("none", f"fill={pct:.2f}>{self.gap_max_fill_pct}")
                results.append(S.scored_three_way("gap_unmitigated", 15, 8, _fill_classify))
            elif f == "retest_quality":
                # Binary 15: bar must wick into the zone but close outside
                # it (rejection wick).  Bull-FVG inverted -> SHORT: high
                # pierces gap_bot but close < gap_bot.  Bear-FVG inverted
                # -> LONG: low pierces gap_top but close > gap_top.
                def _retest():
                    if side is Side.SHORT:
                        if bar.high >= gap_bot and bar.close < gap_bot:
                            wick = bar.high - bar.close
                            body = abs(bar.close - bar.open)
                            if wick > body * 0.5:
                                return True, f"rejection_wick={wick:.2f}>0.5*body"
                            return False, f"weak_rejection(wick={wick:.2f},body={body:.2f})"
                        return False, "no_pierce"
                    else:
                        if bar.low <= gap_top and bar.close > gap_top:
                            wick = bar.close - bar.low
                            body = abs(bar.close - bar.open)
                            if wick > body * 0.5:
                                return True, f"rejection_wick={wick:.2f}>0.5*body"
                            return False, f"weak_rejection(wick={wick:.2f},body={body:.2f})"
                        return False, "no_pierce"
                results.append(S.scored_binary("retest_quality", 15, _retest))
            elif f == "gap_size":
                # Binary 10: gap height >= gap_size_atr * ATR.
                def _gap_size():
                    h = gap_top - gap_bot
                    if atr <= 0:
                        return False, "atr_zero"
                    if h >= self.gap_size_atr * atr:
                        return True, f"gap={h:.2f}>={self.gap_size_atr}*atr"
                    return False, f"gap={h:.2f}<{self.gap_size_atr}*atr"
                results.append(S.scored_binary("gap_size", 10, _gap_size))
            # Unknown filter names silently ignored.

        return S.aggregate_results(
            results,
            full_threshold=self.score_full_threshold,
            half_threshold=self.score_half_threshold,
            log_threshold=self.score_log_threshold,
        )
