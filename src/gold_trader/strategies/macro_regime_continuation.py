"""MacroRegimeContinuationStrategy.

Empirical finding from the 2026-05-08 macro-augmented mining sweep
(reports/mined_patterns/sweep_macro/FINDINGS.md):

The most robust gold-bullish edge across 60m and 240m timeframes is NOT
intraday structure alone — it is a *macro regime conjunction*:

* Falling real yields (real10y down over 5d), AND
* Calm equity vol (VIX 5d-change flat), AND
* Consolidating dollar (DXY flat over 20d), AND
* Optionally: easing fed funds cycle (DFF down over 60d).

Top conjunctions and their cross-TF avg holdout R / min p:

* ``macro_real10y_5d_down & macro_vix_5d_flat``  +1.89  p=0.002
* ``macro_dxy_20d_flat & trend_up``              +1.47  p=0.005
* ``macro_fedfunds_60d_down & macro_real10y_5d_down``  +1.26  p=0.002
* ``macro_dxy_20d_flat & macro_vix_5d_flat``     +1.35  p=0.002

Bearish regime that long-bias must AVOID:

* ``macro_bei10_hi & macro_real10y_lo``  avg_R = -3.44, stability 0/3.

This strategy converts the regime conjunction into a tradable rule.  It
uses the regime as a HARD GATE; intraday structure (EMA trend confirmation
+ bullish bar) only times the entry within the favourable regime window.
Long-only — no short edge survived holdout.

Engine contract
---------------
* Sets ``risk_reward > 0`` so the engine recomputes target from actual fill.
* Once-per-day cap (first eligible bar per UTC date) — the regime persists
  for many bars and we don't want to spam entries.
* Stop = ``stop_atr_mult * ATR`` away from signal-bar close.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Sequence

from ..data.macro import MacroFrame
from ..models import MarketBar, Side, TradeSignal
from .base import lookback_spans_gap


@dataclass(frozen=True)
class MacroRegimeContinuationStrategy:
    """Long-gold continuation gated on a multi-series macro regime.

    Default thresholds reflect FRED units:

    * ``real10y`` (DFII10) is in *percent*; 1 pp = 100 bps.
    * ``dxy`` (DTWEXBGS) is an index level around 95-110.
    * ``vix`` (VIXCLS) is in percent.
    * ``fedfunds`` (DFF) is in percent.
    * ``bei10`` (T10YIE) is in percent.
    """

    macro: MacroFrame  # required

    # Regime thresholds (units match FRED conventions) ------------------
    real_yield_lookback_days: int = 5
    real_yield_max_change_bps: float = 0.0
    """real10y must fall by at least this many bps over the lookback (negative = drop)."""

    dxy_lookback_days: int = 20
    dxy_max_abs_change_pct: float = 0.50
    """DXY 20d % change absolute must be ≤ this (consolidation)."""

    vix_lookback_days: int = 5
    vix_max_change_abs: float = 1.50
    """|ΔVIX over lookback| must be ≤ this (vol-calm regime)."""

    use_fedfunds_relax: bool = False
    """If True, alternative path: fedfunds_60d_change_pct ≤ 0 also satisfies the rates leg."""
    fedfunds_lookback_days: int = 60
    fedfunds_max_change_pct: float = 0.0

    block_stagflation: bool = True
    """If True, skip long when bei10 is in its top tercile AND real10y in bottom tercile."""

    # Intraday timing filters -------------------------------------------
    atr_period: int = 14
    ema_fast: int = 20
    ema_slow: int = 50
    trend_strength_min: float = 0.0
    """EMA fast must exceed EMA slow by at least this fraction (0 = any cross)."""

    stop_atr_mult: float = 1.5
    risk_reward: float = 2.0
    max_spread: float = 1.00
    min_atr_threshold: float = 0.0
    min_news_distance_minutes: float = 30.0
    require_bullish_close: bool = True

    allowed_sessions: tuple[str, ...] = ("london", "new_york")
    name: str = "macro_regime_continuation"

    def warmup_bars(self) -> int:
        return max(self.atr_period, self.ema_slow) + 5

    # ------------------------------------------------------------------
    def signal_for(self, bars: Sequence[MarketBar], index: int) -> TradeSignal | None:
        if index < self.warmup_bars():
            return None
        bar = bars[index]

        # ── basic filters ─────────────────────────────────────────────
        if bar.session not in self.allowed_sessions:
            return None
        if bar.spread > self.max_spread:
            return None
        if (
            bar.news_distance_minutes is not None
            and bar.news_distance_minutes < self.min_news_distance_minutes
        ):
            return None
        if self.require_bullish_close and bar.close <= bar.open:
            return None

        # ── once-per-day gate ─────────────────────────────────────────
        bar_date = bar.timestamp.astimezone(timezone.utc).date()
        if index > 0:
            prev = bars[index - 1]
            prev_date = prev.timestamp.astimezone(timezone.utc).date()
            if prev_date == bar_date and prev.session in self.allowed_sessions:
                return None

        # ── macro regime gate ─────────────────────────────────────────
        if not self._macro_regime_ok(bar):
            return None

        # ── intraday trend confirmation ───────────────────────────────
        if lookback_spans_gap(bars, index, max(self.atr_period, self.ema_slow)):
            return None
        ef = self._ema(bars, index, self.ema_fast)
        es = self._ema(bars, index, self.ema_slow)
        if ef is None or es is None:
            return None
        if ef <= es * (1.0 + self.trend_strength_min):
            return None

        atr = self._atr(bars, index)
        if atr <= 0.0:
            return None
        if self.min_atr_threshold > 0.0 and atr < self.min_atr_threshold:
            return None

        stop_dist = self.stop_atr_mult * atr
        if stop_dist <= 0.0:
            return None

        assumed_entry = bar.close
        stop = assumed_entry - stop_dist
        return TradeSignal(
            side=Side.LONG,
            stop=stop,
            target=assumed_entry + stop_dist * self.risk_reward,
            reason=(
                f"Macro regime long: real10y/dxy/vix favourable, "
                f"EMA{self.ema_fast}>EMA{self.ema_slow}, atr={atr:.2f}, "
                f"session={bar.session}."
            ),
            tags=("macro", "regime_continuation", "long", bar.session),
            risk_reward=self.risk_reward,
        )

    # ------------------------------------------------------------------
    def _macro_regime_ok(self, bar: MarketBar) -> bool:
        ts = bar.timestamp
        m = self.macro

        # Real-yield leg: real10y must be falling.
        real = m.get("real10y")
        if real is None:
            return False
        d_real_pct = real.change(ts, lookback_days=self.real_yield_lookback_days)
        if d_real_pct is None:
            return False
        d_real_bps = d_real_pct * 100.0
        rates_ok = d_real_bps <= self.real_yield_max_change_bps

        # Optional fed-funds alternative path.
        if not rates_ok and self.use_fedfunds_relax:
            ff = m.get("fedfunds")
            if ff is not None:
                d_ff = ff.change(ts, lookback_days=self.fedfunds_lookback_days)
                if d_ff is not None and d_ff <= self.fedfunds_max_change_pct:
                    rates_ok = True
        if not rates_ok:
            return False

        # DXY consolidation leg.
        dxy = m.get("dxy")
        if dxy is None:
            return False
        d_dxy_pct = dxy.pct_change(ts, lookback_days=self.dxy_lookback_days)
        if d_dxy_pct is None:
            return False
        if abs(d_dxy_pct * 100.0) > self.dxy_max_abs_change_pct:
            return False

        # VIX calm leg.
        vix = m.get("vix")
        if vix is None:
            return False
        d_vix = vix.change(ts, lookback_days=self.vix_lookback_days)
        if d_vix is None:
            return False
        if abs(d_vix) > self.vix_max_change_abs:
            return False

        # Stagflation block: bei10 hi AND real10y lo => skip.
        if self.block_stagflation:
            if self._is_stagflation_regime(ts):
                return False

        return True

    def _is_stagflation_regime(self, ts) -> bool:
        m = self.macro
        bei = m.get("bei10")
        real = m.get("real10y")
        if bei is None or real is None:
            return False
        bei_now = bei.as_of(ts)
        real_now = real.as_of(ts)
        if bei_now is None or real_now is None:
            return False
        # Tercile cuts from the cached series points (full history).
        bei_vals = sorted(p.value for p in bei.points)
        real_vals = sorted(p.value for p in real.points)
        if len(bei_vals) < 9 or len(real_vals) < 9:
            return False
        bei_hi_cut = bei_vals[(2 * len(bei_vals)) // 3]
        real_lo_cut = real_vals[len(real_vals) // 3]
        return bei_now >= bei_hi_cut and real_now <= real_lo_cut

    # ------------------------------------------------------------------
    def _atr(self, bars: Sequence[MarketBar], index: int) -> float:
        start = index - self.atr_period + 1
        if start <= 0:
            return 0.0
        atr_bars = bars[start: index + 1]
        previous_close = bars[start - 1].close
        trs: list[float] = []
        for b in atr_bars:
            trs.append(b.true_range(previous_close))
            previous_close = b.close
        return sum(trs) / len(trs) if trs else 0.0

    def _ema(
        self, bars: Sequence[MarketBar], index: int, period: int,
    ) -> float | None:
        if index < period:
            return None
        alpha = 2.0 / (period + 1.0)
        seed_end = index - period + 1
        seed = sum(b.close for b in bars[max(0, seed_end - period): seed_end]) / period
        ema = seed
        for j in range(seed_end, index + 1):
            ema = alpha * bars[j].close + (1.0 - alpha) * ema
        return ema
