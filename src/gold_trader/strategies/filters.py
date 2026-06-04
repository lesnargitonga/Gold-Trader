"""Shared discretionary-checklist filter primitives for all strategies.

Background
----------
The 5y holdout (HANDBOOK §11) showed that mechanical rules without the
operator's discretionary checklist produce sub-1.0 PF.  This module
encodes those filters as small, reusable, independently-toggleable
predicates each returning ``(passed: bool, reason: str)``.

Each strategy declares ``filters_enabled: tuple[str, ...]`` at construction
time.  In ``signal_for``, after generating a candidate signal, the strategy
walks the enabled filters and returns ``None`` (with the rejection reason
attached to ``self._last_filter_rejection`` for ``dump-signals`` near-miss
mode) if any one fails.

Filters are pure functions of ``(bars, index, ...)``.  Heavy series
computations (HTF EMAs, RSI, ATR) are cached per-(id(bars), len(bars))
at module level so they cost O(N) once across an entire backtest.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from ..models import MarketBar, Side


# ---------------------------------------------------------------------------
# Module-level series caches keyed by (id(bars), len(bars), *config).
# Each backtest constructs bars once; id() is stable for the run.
# ---------------------------------------------------------------------------
_EMA_CACHE: dict[tuple, list[float]] = {}
_HTF_EMA_CACHE: dict[tuple, tuple[list[float], list[float], list[int]]] = {}
_MEAN_SPREAD_CACHE: dict[tuple, list[float]] = {}


def _ema(values: list[float], period: int) -> list[float]:
    n = len(values)
    out = [0.0] * n
    if n == 0:
        return out
    k = 2.0 / (period + 1)
    out[0] = values[0]
    for i in range(1, n):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def ema_series(bars: Sequence[MarketBar], period: int) -> list[float]:
    key = (id(bars), len(bars), "ema", period)
    cached = _EMA_CACHE.get(key)
    if cached is None:
        cached = _ema([b.close for b in bars], period)
        _EMA_CACHE[key] = cached
    return cached


# ---------------------------------------------------------------------------
# Higher-timeframe (HTF) helpers — bucket 15m/60m bars into 4H candles
# without resampling the entire dataset.  We compute, for each base-bar
# index i, the EMA20/50 of the *closed* HTF bar that ended at or before
# bars[i].timestamp.
# ---------------------------------------------------------------------------
def htf_ema_aligned(
    bars: Sequence[MarketBar],
    htf_minutes: int = 240,
    fast: int = 20,
    slow: int = 50,
) -> tuple[list[float], list[float], list[int]]:
    """Return (ema_fast, ema_slow, htf_idx_per_bar) aligned to ``bars``.

    ``htf_idx_per_bar[i]`` is the index of the most recent *closed* HTF bucket
    that bar ``i`` saw.  EMA values use only HTF buckets up to that index, so
    no lookahead.  Buckets are aligned to UTC epoch (00:00, 04:00, 08:00, …).
    """
    key = (id(bars), len(bars), htf_minutes, fast, slow)
    cached = _HTF_EMA_CACHE.get(key)
    if cached is not None:
        return cached

    n = len(bars)
    htf_idx_per_bar = [0] * n
    htf_closes: list[float] = []
    htf_bucket_id: list[int] = []  # epoch-bucket id of each closed HTF bar
    cur_bucket = -1
    cur_open = cur_high = cur_low = cur_close = 0.0
    seconds = htf_minutes * 60

    last_closed_idx = -1
    for i, b in enumerate(bars):
        ts = b.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        epoch = int(ts.timestamp())
        bucket = epoch // seconds
        if cur_bucket == -1:
            cur_bucket = bucket
            cur_open = b.open
            cur_high = b.high
            cur_low = b.low
            cur_close = b.close
        elif bucket != cur_bucket:
            # close the previous bucket
            htf_closes.append(cur_close)
            htf_bucket_id.append(cur_bucket)
            last_closed_idx = len(htf_closes) - 1
            cur_bucket = bucket
            cur_open = b.open
            cur_high = b.high
            cur_low = b.low
            cur_close = b.close
        else:
            cur_high = max(cur_high, b.high)
            cur_low = min(cur_low, b.low)
            cur_close = b.close
        htf_idx_per_bar[i] = last_closed_idx

    ema_fast = _ema(htf_closes, fast) if htf_closes else []
    ema_slow = _ema(htf_closes, slow) if htf_closes else []
    cached = (ema_fast, ema_slow, htf_idx_per_bar)
    _HTF_EMA_CACHE[key] = cached
    return cached


def htf_trend_aligned(
    bars: Sequence[MarketBar],
    index: int,
    side: Side,
    htf_minutes: int = 240,
    fast: int = 20,
    slow: int = 50,
) -> tuple[bool, str]:
    """LONG ⇒ HTF EMA(fast) > EMA(slow); SHORT ⇒ opposite.

    Skips if there's not yet a closed HTF bucket (warmup).
    """
    ef, es, idx_map = htf_ema_aligned(bars, htf_minutes, fast, slow)
    h = idx_map[index]
    if h < slow:
        return False, f"htf_warmup(h={h}<{slow})"
    fast_v = ef[h]
    slow_v = es[h]
    if side is Side.LONG and fast_v > slow_v:
        return True, "htf_bull"
    if side is Side.SHORT and fast_v < slow_v:
        return True, "htf_bear"
    return False, f"htf_misaligned(fast={fast_v:.2f},slow={slow_v:.2f},side={side.value})"


# ---------------------------------------------------------------------------
# Session helpers — UTC hour buckets.  Asian = 21..07, London = 07..13,
# NY = 13..21 (gold has no overlap window in this scheme).
# ---------------------------------------------------------------------------
def _utc_hour(bar: MarketBar) -> int:
    ts = bar.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).hour


def session_of(bar: MarketBar) -> str:
    h = _utc_hour(bar)
    if 7 <= h < 13:
        return "london"
    if 13 <= h < 21:
        return "ny"
    return "asia"


def in_session(bar: MarketBar, allowed: tuple[str, ...]) -> tuple[bool, str]:
    s = session_of(bar)
    if s in allowed:
        return True, f"session={s}"
    return False, f"session={s}_not_in_{allowed}"


def hour_window(bar: MarketBar, start_h: int, end_h: int) -> tuple[bool, str]:
    """Inclusive-start, exclusive-end UTC hour window (e.g. 7,10 = 07:00–09:59)."""
    h = _utc_hour(bar)
    if start_h <= h < end_h:
        return True, f"hour={h}"
    return False, f"hour={h}_not_in_[{start_h},{end_h})"


# ---------------------------------------------------------------------------
# Spread / news universal gates
# ---------------------------------------------------------------------------
def mean_spread_series(bars: Sequence[MarketBar], period: int = 60) -> list[float]:
    key = (id(bars), len(bars), "mean_spread", period)
    cached = _MEAN_SPREAD_CACHE.get(key)
    if cached is not None:
        return cached
    n = len(bars)
    out = [0.0] * n
    s = 0.0
    for i, b in enumerate(bars):
        s += b.spread
        if i >= period:
            s -= bars[i - period].spread
        denom = period if i >= period else (i + 1)
        out[i] = s / denom if denom > 0 else 0.0
    _MEAN_SPREAD_CACHE[key] = out
    return out


def spread_relative(
    bars: Sequence[MarketBar], index: int, max_mult: float = 1.2, period: int = 60
) -> tuple[bool, str]:
    ms = mean_spread_series(bars, period)
    bar = bars[index]
    cap = max_mult * ms[index] if ms[index] > 0 else 1e9
    if bar.spread <= cap:
        return True, f"spread={bar.spread:.2f}<=cap={cap:.2f}"
    return False, f"spread={bar.spread:.2f}>cap={cap:.2f}"


def news_clear(bar: MarketBar, min_minutes: int = 15) -> tuple[bool, str]:
    nd = bar.news_distance_minutes
    if nd is None:
        return True, "news=unknown"
    if abs(nd) >= min_minutes:
        return True, f"news_distance={nd:.0f}min"
    return False, f"news_too_close({nd:.0f}min)"


def weekend_clear(bar: MarketBar, friday_close_h: int = 21) -> tuple[bool, str]:
    ts = bar.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    # Friday = 4 (Mon=0). Block 60min before Friday close.
    if ts.weekday() == 4 and ts.hour >= friday_close_h - 1:
        return False, f"friday_close_proximity({ts.hour:02d}:{ts.minute:02d})"
    if ts.weekday() == 5:
        return False, "saturday"
    if ts.weekday() == 6 and ts.hour < 22:
        return False, "sunday_pre_open"
    return True, "weekend_clear"


# ---------------------------------------------------------------------------
# Structural filters
# ---------------------------------------------------------------------------
def prior_swing_swept(
    bars: Sequence[MarketBar],
    index: int,
    side: Side,
    lookback: int = 20,
    pivot_window: int = 3,
) -> tuple[bool, str]:
    """For a LONG signal, did price within the last ``lookback`` bars take out
    a prior pivot LOW (sweep liquidity below) and then close back above it?
    Mirror for SHORT: sweep above a pivot HIGH then close back below."""
    if index < lookback + pivot_window:
        return False, "sweep_warmup"
    start = max(pivot_window, index - lookback)
    if side is Side.LONG:
        # find a confirmed pivot low in the window
        for i in range(index - 1, start - 1, -1):
            if _is_pivot_low(bars, i, pivot_window):
                pivot_low = bars[i].low
                # any bar after pivot dipped below pivot_low and any later bar closed above it?
                swept_at = None
                for j in range(i + pivot_window, index + 1):
                    if bars[j].low < pivot_low:
                        swept_at = j
                        break
                if swept_at is not None:
                    for k in range(swept_at, index + 1):
                        if bars[k].close > pivot_low:
                            return True, f"sweep_low@{i}->reclaim@{k}"
        return False, "no_low_sweep"
    else:
        for i in range(index - 1, start - 1, -1):
            if _is_pivot_high(bars, i, pivot_window):
                pivot_high = bars[i].high
                swept_at = None
                for j in range(i + pivot_window, index + 1):
                    if bars[j].high > pivot_high:
                        swept_at = j
                        break
                if swept_at is not None:
                    for k in range(swept_at, index + 1):
                        if bars[k].close < pivot_high:
                            return True, f"sweep_high@{i}->reclaim@{k}"
        return False, "no_high_sweep"


def _is_pivot_low(bars: Sequence[MarketBar], i: int, w: int) -> bool:
    if i - w < 0 or i + w >= len(bars):
        return False
    lo = bars[i].low
    for j in range(i - w, i + w + 1):
        if j == i:
            continue
        if bars[j].low <= lo:
            return False
    return True


def _is_pivot_high(bars: Sequence[MarketBar], i: int, w: int) -> bool:
    if i - w < 0 or i + w >= len(bars):
        return False
    hi = bars[i].high
    for j in range(i - w, i + w + 1):
        if j == i:
            continue
        if bars[j].high >= hi:
            return False
    return True


def displacement_min(
    bar: MarketBar, atr: float, min_atr: float = 1.5, body_pct: float = 0.0
) -> tuple[bool, str]:
    """Bar body must be >= min_atr × ATR.  Optional body_pct: body / total range."""
    body = abs(bar.close - bar.open)
    rng = max(bar.high - bar.low, 1e-9)
    if body < min_atr * atr:
        return False, f"body={body:.2f}<{min_atr}*atr={min_atr * atr:.2f}"
    if body_pct > 0 and (body / rng) < body_pct:
        return False, f"body_pct={body / rng:.2f}<{body_pct}"
    return True, f"displacement_ok(body={body:.2f},atr={atr:.2f})"


def gap_recency(
    formation_idx: int, current_idx: int, max_bars: int = 12
) -> tuple[bool, str]:
    age = current_idx - formation_idx
    if age <= max_bars:
        return True, f"age={age}"
    return False, f"stale(age={age}>{max_bars})"


def gap_unmitigated(
    bars: Sequence[MarketBar],
    formation_idx: int,
    current_idx: int,
    bot: float,
    top: float,
    max_fill_pct: float = 0.30,
) -> tuple[bool, str]:
    """Between formation and now (excluding the current retest bar), price
    must not have wicked into >max_fill_pct of the gap body."""
    width = top - bot
    if width <= 0:
        return False, "zero_width"
    deepest = 0.0
    for i in range(formation_idx + 1, current_idx):
        b = bars[i]
        # how deep into the zone?
        penetration = max(0.0, min(top, b.high) - max(bot, b.low))
        if penetration > deepest:
            deepest = penetration
    pct = deepest / width
    if pct <= max_fill_pct:
        return True, f"fill={pct:.2f}"
    return False, f"pre_filled({pct:.2f}>{max_fill_pct})"


def min_swing_size(
    bars: Sequence[MarketBar],
    pivot_idx: int,
    side: Side,
    atr: float,
    min_atr: float = 1.0,
    lookback: int = 20,
) -> tuple[bool, str]:
    """For LONG (bullish divergence): pivot LOW must be >= min_atr*atr below
    the prior `lookback`-bar low.  Mirror for SHORT."""
    start = max(0, pivot_idx - lookback)
    window = bars[start:pivot_idx]
    if not window:
        return False, "swing_warmup"
    if side is Side.LONG:
        prior_low = min(b.low for b in window)
        delta = prior_low - bars[pivot_idx].low
        if delta >= min_atr * atr:
            return True, f"swing_delta={delta:.2f}>={min_atr}*atr"
        return False, f"swing_delta={delta:.2f}<{min_atr}*atr={min_atr * atr:.2f}"
    else:
        prior_high = max(b.high for b in window)
        delta = bars[pivot_idx].high - prior_high
        if delta >= min_atr * atr:
            return True, f"swing_delta={delta:.2f}>={min_atr}*atr"
        return False, f"swing_delta={delta:.2f}<{min_atr}*atr={min_atr * atr:.2f}"


def confirmation_timing(
    pivot_idx: int, signal_idx: int, max_bars: int = 2
) -> tuple[bool, str]:
    delta = signal_idx - pivot_idx
    if 0 <= delta <= max_bars:
        return True, f"confirmation_lag={delta}"
    return False, f"confirmation_lag={delta}>{max_bars}"


def dxy_aligned(
    bars: Sequence[MarketBar],
    index: int,
    side: Side,
    lookback: int = 20,
) -> tuple[bool, str]:
    """For LONG gold: DXY should not be in a strong uptrend (recent slope > 0
    by a meaningful amount).  Mirror for SHORT."""
    if index < lookback:
        return True, "dxy_warmup_pass"  # don't gate during warmup
    end = bars[index].dxy_close
    start = bars[index - lookback].dxy_close
    if end is None or start is None:
        return True, "dxy_unknown_pass"  # if no DXY data, don't gate
    if start == 0:
        return True, "dxy_zero_pass"
    pct = (end - start) / start
    threshold = 0.005  # 0.5% over `lookback` bars
    if side is Side.LONG and pct > threshold:
        return False, f"dxy_strong_up({pct * 100:.2f}%)"
    if side is Side.SHORT and pct < -threshold:
        return False, f"dxy_strong_down({pct * 100:.2f}%)"
    return True, f"dxy_ok({pct * 100:.2f}%)"


def not_overextended(
    bars: Sequence[MarketBar],
    index: int,
    side: Side,
    lookback: int = 5,
) -> tuple[bool, str]:
    """Block momentum entries that follow N consecutive bars in the same direction."""
    if index < lookback:
        return True, "overextend_warmup"
    same = 0
    sign = 1 if side is Side.LONG else -1
    for i in range(index - lookback, index):
        d = bars[i].close - bars[i].open
        if (d > 0 and sign > 0) or (d < 0 and sign < 0):
            same += 1
        else:
            same = 0
    if same >= lookback:
        return False, f"extended_run({same}_consec)"
    return True, f"not_extended(consec={same})"


# ---------------------------------------------------------------------------
# Cached ATR series — used by the universal scorer below.
# ---------------------------------------------------------------------------
_ATR_CACHE: dict[tuple, list[float]] = {}


def atr_series_cached(bars: Sequence[MarketBar], period: int = 14) -> list[float]:
    """Wilder ATR aligned to ``bars`` (bars[0..period-1] return 0.0)."""
    key = (id(bars), len(bars), "atr", period)
    cached = _ATR_CACHE.get(key)
    if cached is not None:
        return cached
    n = len(bars)
    out = [0.0] * n
    if n < 2:
        _ATR_CACHE[key] = out
        return out
    trs = [0.0] * n
    for i in range(1, n):
        prev_close = bars[i - 1].close
        h, l = bars[i].high, bars[i].low
        trs[i] = max(h - l, abs(h - prev_close), abs(l - prev_close))
    if n > period:
        seed = sum(trs[1 : period + 1]) / period
        out[period] = seed
        prev = seed
        for i in range(period + 1, n):
            prev = (prev * (period - 1) + trs[i]) / period
            out[i] = prev
    _ATR_CACHE[key] = out
    return out


# ---------------------------------------------------------------------------
# Universal scoring — strategy-agnostic confluence score 0..100.
#
# Every strategy's signal can be passed through this scorer to attach a
# diagnostic confidence score *without* changing the strategy's own
# pass/reject logic.  Used for live operator dashboards, ensemble
# weighting, and post-hoc bucket analytics.  The features chosen here
# have empirically uncontroversial sign on XAUUSD intraday:
#
#   1. htf_alignment  (binary)  20pts — HTF EMA20>EMA50 agrees with side
#   2. news_clear     (binary)  15pts — >= 30min from scheduled news
#   3. weekend_clear  (binary)  10pts — not in Friday-late / weekend
#   4. session_quality(3-way)   20pts — full London/NY core, partial NY-late
#   5. spread_quality (binary)  15pts — spread <= 1.2x rolling mean
#   6. atr_regime    (3-way)    10pts — ATR within healthy band
#   7. not_overextended(binary) 10pts — last 5 bars not all same direction
#
# Total max = 100.  No strategy-specific filters here — those live in
# the strategy module.
# ---------------------------------------------------------------------------
def universal_score(
    bars: Sequence[MarketBar],
    index: int,
    side: Side,
    *,
    htf_minutes: int = 240,
    htf_fast: int = 20,
    htf_slow: int = 50,
    atr_period: int = 14,
    atr_low: float = 0.5,
    atr_high: float = 6.0,
):
    """Compute the universal diagnostic SignalScore for a candidate signal.

    Returns a ``SignalScore`` (see ``scoring.py``) — never ``None``.  The
    caller decides whether to gate on the verdict; the default policy is
    to attach the score to the TradeSignal but not change pass/fail.
    """
    # Imported here to avoid an import cycle at module load.
    from . import scoring as S

    bar = bars[index]
    results: list[S.FilterResult] = []

    # 1. HTF alignment
    results.append(S.scored_binary(
        "u_htf_alignment", 20,
        lambda: htf_trend_aligned(
            bars, index, side,
            htf_minutes=htf_minutes, fast=htf_fast, slow=htf_slow,
        ),
    ))

    # 2. News clear
    results.append(S.scored_binary(
        "u_news_clear", 15,
        lambda: news_clear(bar, min_minutes=30),
    ))

    # 3. Weekend clear
    results.append(S.scored_binary(
        "u_weekend_clear", 10,
        lambda: weekend_clear(bar),
    ))

    # 4. Session quality (3-way)
    def _session_classify():
        h = _utc_hour(bar)
        if 7 <= h < 10 or 13 <= h < 17:
            return "full", f"core_session(h={h})"
        if 10 <= h < 13 or 17 <= h < 21:
            return "partial", f"edge_session(h={h})"
        return "none", f"off_session(h={h})"
    results.append(S.scored_three_way(
        "u_session_quality", 20, 10, _session_classify,
    ))

    # 5. Spread quality
    results.append(S.scored_binary(
        "u_spread_quality", 15,
        lambda: spread_relative(bars, index, max_mult=1.2, period=60),
    ))

    # 6. ATR regime (3-way)
    atr = atr_series_cached(bars, atr_period)[index]
    def _atr_classify():
        if atr <= 0:
            return "none", "atr_unknown"
        if atr_low <= atr <= atr_high:
            return "full", f"atr_healthy({atr:.2f})"
        if atr_low * 0.5 <= atr < atr_low or atr_high < atr <= atr_high * 1.5:
            return "partial", f"atr_edge({atr:.2f})"
        return "none", f"atr_extreme({atr:.2f})"
    results.append(S.scored_three_way(
        "u_atr_regime", 10, 5, _atr_classify,
    ))

    # 7. Not overextended
    results.append(S.scored_binary(
        "u_not_overextended", 10,
        lambda: not_overextended(bars, index, side, lookback=5),
    ))

    return S.aggregate_results(results)


def reset_caches() -> None:
    """Test hook — clear module-level caches."""
    _EMA_CACHE.clear()
    _HTF_EMA_CACHE.clear()
    _MEAN_SPREAD_CACHE.clear()
    _ATR_CACHE.clear()


# ---------------------------------------------------------------------------
# Shared filter dispatcher used by strategies that don't need bespoke
# pivot/gap arguments — supports htf_trend / htf_counter / dxy / news /
# weekend / spread_relative / not_overextended / entry_session / hour_window.
# Strategies pass kwargs via the ``cfg`` dict.
# ---------------------------------------------------------------------------
def apply_generic_filters(
    bars: Sequence[MarketBar],
    index: int,
    side: Side,
    filters_enabled: tuple[str, ...],
    cfg: dict | None = None,
) -> tuple[bool, str]:
    cfg = cfg or {}
    bar = bars[index]
    for f in filters_enabled:
        if f == "htf_trend":
            ok, why = htf_trend_aligned(
                bars, index, side,
                htf_minutes=cfg.get("htf_minutes", 240),
                fast=cfg.get("htf_ema_fast", 20),
                slow=cfg.get("htf_ema_slow", 50),
            )
        elif f == "htf_counter":
            opp = Side.SHORT if side is Side.LONG else Side.LONG
            ok, why = htf_trend_aligned(
                bars, index, opp,
                htf_minutes=cfg.get("htf_minutes", 240),
                fast=cfg.get("htf_ema_fast", 20),
                slow=cfg.get("htf_ema_slow", 50),
            )
        elif f == "dxy":
            ok, why = dxy_aligned(bars, index, side, lookback=cfg.get("dxy_lookback", 20))
        elif f == "news":
            ok, why = news_clear(bar, min_minutes=cfg.get("news_min_minutes", 15))
        elif f == "weekend":
            ok, why = weekend_clear(bar)
        elif f == "spread_relative":
            ok, why = spread_relative(
                bars, index,
                max_mult=cfg.get("spread_max_mult", 1.2),
                period=cfg.get("spread_period", 60),
            )
        elif f == "not_overextended":
            ok, why = not_overextended(
                bars, index, side, lookback=cfg.get("overextend_lookback", 5),
            )
        elif f == "entry_session":
            ok, why = in_session(bar, allowed=cfg.get("allowed_sessions", ("london", "ny")))
        elif f == "hour_window":
            ok, why = hour_window(bar, cfg["hour_start"], cfg["hour_end"])
        else:
            continue
        if not ok:
            return False, f"{f}:{why}"
    return True, "all_pass"
