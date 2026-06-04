"""Boolean feature vocabulary over a bar series.

Each feature is a name → ``list[bool]`` of equal length to the input bars.
Features are *forward-safe*: feature[i] uses only data from bars[0..i],
never from bars[i+1..].  The pattern miner combines these into rules.

Categories (each mutually-exclusive bucket is exposed as N booleans):

* Volatility regime    — atr_q0..q4   (ATR percentile within trailing window)
* Trend regime         — trend_up / trend_flat / trend_down  (EMA20 vs EMA50)
* Distance to 20 EMA   — close_above_ema20 / close_below_ema20
* Bar shape            — bull_close / bear_close / doji_body
* Body size            — body_q0..q4  (|close-open|/ATR percentile)
* Range size           — range_q0..q4 ((high-low)/ATR percentile)
* Recent momentum      — ret5_up / ret5_flat / ret5_down  (5-bar return tertiles)
* Higher-high / lower-low (vs prior 10 bars)
* Session              — session_asia / session_london / session_ny / session_other
* Day of week          — dow_mon..dow_fri
* Hour bucket          — hour_q0..q3

Total typical vocabulary: ~50 features.  All produced in a single pass
through the bars.

Pure stdlib.  No numpy dependency.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Sequence

from ..models import MarketBar


@dataclass(frozen=True)
class FeatureMatrix:
    """Container: feature name → bool list (one bool per bar)."""

    bar_count: int
    features: dict[str, list[bool]]

    def names(self) -> list[str]:
        return sorted(self.features.keys())

    def vector(self, name: str) -> list[bool]:
        return self.features[name]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ema(values: Sequence[float], period: int) -> list[float | None]:
    """Standard exponential moving average — None until period-1 bars."""
    if period <= 0:
        raise ValueError("period > 0")
    out: list[float | None] = [None] * len(values)
    if not values:
        return out
    alpha = 2.0 / (period + 1.0)
    # Seed with simple mean of first ``period`` bars.
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = alpha * values[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def _atr(bars: Sequence[MarketBar], period: int) -> list[float | None]:
    """Wilder ATR — None until period bars accumulated."""
    out: list[float | None] = [None] * len(bars)
    if len(bars) < period + 1:
        return out
    trs: list[float] = []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        tr = max(
            b.high - b.low,
            abs(b.high - p.close),
            abs(b.low - p.close),
        )
        trs.append(tr)
    # Initial ATR = simple mean of first `period` TRs (i.e. up to bar `period`).
    seed = sum(trs[:period]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, len(bars)):
        tr_i = trs[i - 1]
        prev = (prev * (period - 1) + tr_i) / period
        out[i] = prev
    return out


def _percentile_rank(window: list[float], value: float) -> float:
    """Rank of `value` within `window`, in [0,1].  Inclusive of self."""
    if not window:
        return 0.5
    sorted_w = sorted(window)
    pos = bisect.bisect_right(sorted_w, value)
    return pos / len(sorted_w)


def _bucket(rank: float, n: int) -> int:
    """Map a rank in [0,1] to one of n buckets [0..n-1]."""
    idx = int(rank * n)
    if idx >= n:
        idx = n - 1
    if idx < 0:
        idx = 0
    return idx


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_feature_matrix(
    bars: Sequence[MarketBar],
    *,
    atr_period: int = 14,
    rolling_window: int = 100,
) -> FeatureMatrix:
    """Compute the canonical boolean feature vocabulary.

    Args:
      bars: input series, oldest → newest.
      atr_period: ATR smoothing window (Wilder).
      rolling_window: trailing window for percentile rank features.
    """
    n = len(bars)
    feats: dict[str, list[bool]] = {}

    closes = [b.close for b in bars]
    opens = [b.open for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]

    # Trend
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    feats["trend_up"] = [
        bool(e20 is not None and e50 is not None and e20 > e50 * 1.0005)
        for e20, e50 in zip(ema20, ema50)
    ]
    feats["trend_down"] = [
        bool(e20 is not None and e50 is not None and e20 < e50 * 0.9995)
        for e20, e50 in zip(ema20, ema50)
    ]
    feats["trend_flat"] = [
        not (u or d)
        for u, d in zip(feats["trend_up"], feats["trend_down"])
    ]

    feats["close_above_ema20"] = [
        bool(e is not None and c > e) for c, e in zip(closes, ema20)
    ]
    feats["close_below_ema20"] = [
        bool(e is not None and c < e) for c, e in zip(closes, ema20)
    ]

    # ATR + ATR percentile bucket
    atr_vals = _atr(bars, atr_period)

    atr_q = [-1] * n
    body_q = [-1] * n
    range_q = [-1] * n
    body_norm = [0.0] * n
    range_norm = [0.0] * n
    for i in range(n):
        a = atr_vals[i]
        if a is None or a <= 0:
            continue
        body_norm[i] = abs(closes[i] - opens[i]) / a
        range_norm[i] = (highs[i] - lows[i]) / a

    # Rolling percentile buckets — single backward pass, keep a window.
    for i in range(n):
        if atr_vals[i] is None:
            continue
        lo = max(0, i - rolling_window + 1)
        win_a = [atr_vals[j] for j in range(lo, i) if atr_vals[j] is not None]
        if win_a:
            atr_q[i] = _bucket(_percentile_rank(win_a, atr_vals[i]), 5)
        win_b = [body_norm[j] for j in range(lo, i) if atr_vals[j] is not None]
        if win_b:
            body_q[i] = _bucket(_percentile_rank(win_b, body_norm[i]), 5)
        win_r = [range_norm[j] for j in range(lo, i) if atr_vals[j] is not None]
        if win_r:
            range_q[i] = _bucket(_percentile_rank(win_r, range_norm[i]), 5)

    for k in range(5):
        feats[f"atr_q{k}"] = [q == k for q in atr_q]
        feats[f"body_q{k}"] = [q == k for q in body_q]
        feats[f"range_q{k}"] = [q == k for q in range_q]

    # Bar shape
    feats["bull_close"] = [closes[i] > opens[i] for i in range(n)]
    feats["bear_close"] = [closes[i] < opens[i] for i in range(n)]
    feats["doji_body"] = [body_norm[i] < 0.10 and atr_vals[i] is not None for i in range(n)]

    # 5-bar return tertiles
    ret5: list[float | None] = [None] * n
    for i in range(5, n):
        prev = closes[i - 5]
        if prev > 0:
            ret5[i] = (closes[i] - prev) / prev
    # Use a fixed threshold of 0.2% — keeps the buckets stable and
    # interpretable across regimes.
    feats["ret5_up"] = [bool(r is not None and r > 0.002) for r in ret5]
    feats["ret5_down"] = [bool(r is not None and r < -0.002) for r in ret5]
    feats["ret5_flat"] = [
        bool(r is not None) and not u and not d
        for r, u, d in zip(ret5, feats["ret5_up"], feats["ret5_down"])
    ]

    # Higher-high / lower-low vs prior 10
    feats["higher_high_10"] = [False] * n
    feats["lower_low_10"] = [False] * n
    for i in range(10, n):
        prior_high = max(highs[i - 10:i])
        prior_low = min(lows[i - 10:i])
        feats["higher_high_10"][i] = highs[i] > prior_high
        feats["lower_low_10"][i] = lows[i] < prior_low

    # Session
    sessions = [b.session for b in bars]
    for s in ("asia", "london", "new_york"):
        key = f"session_{s.replace('new_york', 'ny')}"
        feats[key] = [v == s for v in sessions]
    feats["session_other"] = [
        s not in ("asia", "london", "new_york") for s in sessions
    ]

    # Day of week (UTC)
    dows = [b.timestamp.weekday() for b in bars]
    for d, name in enumerate(("mon", "tue", "wed", "thu", "fri")):
        feats[f"dow_{name}"] = [v == d for v in dows]

    # Hour bucket (4 buckets of 6h each, UTC).
    hours = [b.timestamp.hour for b in bars]
    for k in range(4):
        lo, hi = k * 6, (k + 1) * 6
        feats[f"hour_q{k}"] = [lo <= h < hi for h in hours]

    # Finer hour octants (3h each, UTC) — captures the 18:00-21:00 vs
    # 21:00-24:00 distinction inside what the quartiles call hour_q3.
    for k in range(8):
        lo, hi = k * 3, (k + 1) * 3
        feats[f"hour_o{k}"] = [lo <= h < hi for h in hours]

    # Month bucket (calendar quarter — captures large seasonality).
    months = [b.timestamp.month for b in bars]
    for q, lo_m in enumerate((1, 4, 7, 10)):
        feats[f"month_q{q}"] = [lo_m <= m < lo_m + 3 for m in months]

    # ------------------------------------------------------------------
    # Multi-bar shapes & gaps (relative to previous bar)
    # ------------------------------------------------------------------
    feats["inside_bar"] = [False] * n
    feats["outside_bar"] = [False] * n
    feats["gap_up"] = [False] * n
    feats["gap_down"] = [False] * n
    for i in range(1, n):
        a = atr_vals[i] or 0.0
        ph, pl, pc = highs[i - 1], lows[i - 1], closes[i - 1]
        if highs[i] < ph and lows[i] > pl:
            feats["inside_bar"][i] = True
        if highs[i] > ph and lows[i] < pl:
            feats["outside_bar"][i] = True
        if a > 0:
            if opens[i] - pc > 0.30 * a:
                feats["gap_up"][i] = True
            if pc - opens[i] > 0.30 * a:
                feats["gap_down"][i] = True

    # Wick dominance (relative to body).
    feats["wick_up_dom"] = [False] * n
    feats["wick_down_dom"] = [False] * n
    for i in range(n):
        a = atr_vals[i] or 0.0
        if a <= 0:
            continue
        body_top = max(opens[i], closes[i])
        body_bot = min(opens[i], closes[i])
        upper = highs[i] - body_top
        lower = body_bot - lows[i]
        body = body_top - body_bot
        if upper > 2 * lower and upper > 0.5 * a and upper > body:
            feats["wick_up_dom"][i] = True
        if lower > 2 * upper and lower > 0.5 * a and lower > body:
            feats["wick_down_dom"][i] = True

    # Three consecutive directional closes.
    feats["consec_up_3"] = [False] * n
    feats["consec_down_3"] = [False] * n
    for i in range(2, n):
        if (closes[i] > opens[i]
                and closes[i - 1] > opens[i - 1]
                and closes[i - 2] > opens[i - 2]):
            feats["consec_up_3"][i] = True
        if (closes[i] < opens[i]
                and closes[i - 1] < opens[i - 1]
                and closes[i - 2] < opens[i - 2]):
            feats["consec_down_3"][i] = True

    # Volatility regime trend (ATR now vs ATR 10 bars ago).
    feats["vol_expanding"] = [False] * n
    feats["vol_contracting"] = [False] * n
    for i in range(10, n):
        a_now = atr_vals[i]
        a_then = atr_vals[i - 10]
        if a_now is None or a_then is None or a_then <= 0:
            continue
        if a_now > 1.20 * a_then:
            feats["vol_expanding"][i] = True
        if a_now < 0.80 * a_then:
            feats["vol_contracting"][i] = True

    # Proximity to local extremes (within 0.30 ATR of the 20-bar high/low).
    feats["near_20_high"] = [False] * n
    feats["near_20_low"] = [False] * n
    for i in range(20, n):
        a = atr_vals[i] or 0.0
        if a <= 0:
            continue
        hh = max(highs[i - 20:i])
        ll = min(lows[i - 20:i])
        if hh - closes[i] < 0.30 * a:
            feats["near_20_high"][i] = True
        if closes[i] - ll < 0.30 * a:
            feats["near_20_low"][i] = True

    # Distance from EMA20 in ATR units — trend stretch.
    feats["far_above_ema20"] = [False] * n
    feats["far_below_ema20"] = [False] * n
    for i in range(n):
        a = atr_vals[i] or 0.0
        e = ema20[i]
        if a <= 0 or e is None:
            continue
        d = (closes[i] - e) / a
        if d > 1.0:
            feats["far_above_ema20"][i] = True
        if d < -1.0:
            feats["far_below_ema20"][i] = True

    # Cross-timeframe HTF context — 4× resampled trend.
    # For 15m bars this gives an "hourly" trend overlay; for 60m bars,
    # a "4-hour" trend overlay; etc.  Always rolling mean of last 4
    # closes vs last 16 closes (≈ EMA20/EMA50 on the higher TF).
    feats["htf_trend_up"] = [False] * n
    feats["htf_trend_down"] = [False] * n
    for i in range(16, n):
        short = sum(closes[i - 4:i]) / 4
        long_ = sum(closes[i - 16:i]) / 16
        if short > long_ * 1.0010:
            feats["htf_trend_up"][i] = True
        elif short < long_ * 0.9990:
            feats["htf_trend_down"][i] = True

    # Sanity check — every vector must be the right length.
    for name, vec in feats.items():
        assert len(vec) == n, f"feature {name} length mismatch"

    return FeatureMatrix(bar_count=n, features=feats)
