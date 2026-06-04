"""Tests for the feature vocabulary and pattern miner."""
from __future__ import annotations

import math
import random
import unittest
from datetime import datetime, timedelta, timezone

from gold_trader.models import MarketBar
from gold_trader.research.features import build_feature_matrix
from gold_trader.research.pattern_miner import (
    MinedPattern,
    MinerConfig,
    _benjamini_hochberg,
    _bh_adjust,
    _block_bootstrap_p,
    _forward_returns,
    mine_patterns,
)


def _bar(t: datetime, o: float, h: float, l: float, c: float,
         session: str = "london") -> MarketBar:
    return MarketBar(
        timestamp=t, open=o, high=h, low=l, close=c, volume=1.0,
        spread=0.5, session=session, news_distance_minutes=None,
        dxy_close=None,
    )


def _synthetic_series(n: int, *, seed: int = 1) -> list[MarketBar]:
    """Geometric-Brownian-ish series with weekly trend regime."""
    rng = random.Random(seed)
    bars: list[MarketBar] = []
    price = 4000.0
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for i in range(n):
        ts = t0 + timedelta(minutes=15 * i)
        # weak trend that flips every 200 bars
        drift = 0.0002 if (i // 200) % 2 == 0 else -0.0002
        ret = drift + rng.gauss(0.0, 0.001)
        new_price = max(1.0, price * (1.0 + ret))
        hi = max(price, new_price) * (1 + abs(rng.gauss(0, 0.0005)))
        lo = min(price, new_price) * (1 - abs(rng.gauss(0, 0.0005)))
        # Map UTC hour to session.
        h = ts.hour
        if h < 7:
            sess = "asia"
        elif h < 13:
            sess = "london"
        elif h < 21:
            sess = "new_york"
        else:
            sess = "asia"
        bars.append(_bar(ts, price, hi, lo, new_price, session=sess))
        price = new_price
    return bars


# ---------------------------------------------------------------------------
# FeatureMatrix
# ---------------------------------------------------------------------------


class FeatureMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bars = _synthetic_series(500)
        self.fm = build_feature_matrix(self.bars)

    def test_every_vector_matches_bar_count(self) -> None:
        for name in self.fm.names():
            self.assertEqual(len(self.fm.vector(name)), len(self.bars))

    def test_trend_and_session_are_mutually_exclusive(self) -> None:
        for i in range(60, len(self.bars)):
            up = self.fm.vector("trend_up")[i]
            dn = self.fm.vector("trend_down")[i]
            fl = self.fm.vector("trend_flat")[i]
            self.assertEqual(int(up) + int(dn) + int(fl), 1, f"i={i}")
        for i in range(len(self.bars)):
            cnt = sum(self.fm.vector(f"session_{s}")[i]
                      for s in ("asia", "london", "ny", "other"))
            self.assertEqual(cnt, 1, f"i={i}")

    def test_atr_buckets_cover_5_quintiles(self) -> None:
        # Beyond rolling_window we should see all 5 buckets used.
        seen = set()
        for i in range(120, len(self.bars)):
            for k in range(5):
                if self.fm.vector(f"atr_q{k}")[i]:
                    seen.add(k)
        self.assertEqual(seen, {0, 1, 2, 3, 4})

    def test_no_lookahead_in_trend(self) -> None:
        """trend_up at bar i must not depend on bars > i."""
        # Build matrix on first half only; the prefix should match the
        # first-half values from the full-series matrix.
        half = len(self.bars) // 2
        fm_half = build_feature_matrix(self.bars[:half])
        for i in range(half):
            self.assertEqual(
                fm_half.vector("trend_up")[i],
                self.fm.vector("trend_up")[i],
                f"trend_up mismatch at i={i}",
            )


# ---------------------------------------------------------------------------
# Statistics primitives
# ---------------------------------------------------------------------------


class StatTests(unittest.TestCase):
    def test_bh_reject_basic(self) -> None:
        # Mix of clear hits and noise.
        pvals = [0.001, 0.01, 0.05, 0.5, 0.9]
        rej = _benjamini_hochberg(pvals, q=0.10)
        # Top 3 should be rejected (BH thresholds: 0.02, 0.04, 0.06,
        # 0.08, 0.10).  Iterating from largest down: 0.05 ≤ 0.06 hits,
        # so all three smaller p's also reject.
        self.assertEqual(rej, [True, True, True, False, False])

    def test_bh_adjust_monotone(self) -> None:
        pvals = [0.001, 0.01, 0.05, 0.5, 0.9]
        adj = _bh_adjust(pvals)
        # Adjusted p-values should be monotonically non-decreasing in
        # the original p-value order.
        ordered = sorted(zip(pvals, adj))
        prev = -1.0
        for _p, a in ordered:
            self.assertGreaterEqual(a, prev)
            prev = a
        self.assertLessEqual(adj[-1], 1.0)

    def test_block_bootstrap_zero_mean_high_p(self) -> None:
        rng = random.Random(0)
        xs = [rng.gauss(0.0, 1.0) for _ in range(400)]
        p = _block_bootstrap_p(xs, blocks=400, block_size=8, rng=rng)
        self.assertGreater(p, 0.05)

    def test_block_bootstrap_strong_signal_low_p(self) -> None:
        rng = random.Random(0)
        # +0.3 mean shift, sd 1.0, n=400 → t≈6, p should be tiny.
        xs = [0.3 + rng.gauss(0.0, 1.0) for _ in range(400)]
        p = _block_bootstrap_p(xs, blocks=400, block_size=8, rng=rng)
        self.assertLess(p, 0.05)


# ---------------------------------------------------------------------------
# Forward returns
# ---------------------------------------------------------------------------


class ForwardReturnTests(unittest.TestCase):
    def test_forward_returns_no_lookahead_at_tail(self) -> None:
        bars = _synthetic_series(200)
        fwd = _forward_returns(bars, horizon=5)
        # Last `horizon` entries must be None (no future data available).
        for i in range(len(bars) - 5, len(bars)):
            self.assertIsNone(fwd[i])
        # Some earlier entries should be defined.
        defined = [r for r in fwd if r is not None]
        self.assertGreater(len(defined), 100)


# ---------------------------------------------------------------------------
# Miner end-to-end
# ---------------------------------------------------------------------------


class MinerTests(unittest.TestCase):
    def test_runs_without_finding_spurious_patterns_on_random_walk(self) -> None:
        # Pure random walk → BH-FDR should reject most things at q=0.10.
        # With bootstrap_blocks=200 the p-value resolution is ~1/201
        # so a handful of weak survivors is expected; the count should
        # remain well below the size of the candidate set (~50 features).
        bars = _synthetic_series(1500, seed=42)
        fm = build_feature_matrix(bars)
        cfg = MinerConfig(
            horizon_bars=4,
            max_combo_size=1,
            min_signals=50,
            min_effect_r=0.05,
            fdr_q=0.10,
            bootstrap_blocks=200,
            block_size=8,
        )
        out = mine_patterns(bars, fm, config=cfg)
        # ~50 candidate features; FDR should keep survivors well under 15.
        self.assertLess(len(out), 15)

    def test_finds_implanted_signal(self) -> None:
        """Implant a clean session-conditional effect and check it survives."""
        rng = random.Random(7)
        n = 3000
        bars: list[MarketBar] = []
        price = 4000.0
        t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for i in range(n):
            ts = t0 + timedelta(minutes=15 * i)
            h = ts.hour
            if h < 7:
                sess = "asia"
            elif h < 13:
                sess = "london"
            elif h < 21:
                sess = "new_york"
            else:
                sess = "asia"
            ret = rng.gauss(0.0, 0.001)
            # Implant: NY session gets a +0.0010 drift bias on the bar.
            if sess == "new_york":
                ret += 0.0010
            new_price = max(1.0, price * (1.0 + ret))
            hi = max(price, new_price) * (1 + abs(rng.gauss(0, 0.0003)))
            lo = min(price, new_price) * (1 - abs(rng.gauss(0, 0.0003)))
            bars.append(_bar(ts, price, hi, lo, new_price, session=sess))
            price = new_price
        fm = build_feature_matrix(bars)
        cfg = MinerConfig(
            horizon_bars=4,
            max_combo_size=1,
            min_signals=100,
            min_effect_r=0.05,
            fdr_q=0.10,
            bootstrap_blocks=300,
            block_size=8,
            holdout_min_signals=30,
        )
        out = mine_patterns(bars, fm, config=cfg)
        self.assertGreater(len(out), 0, "expected at least one survivor")
        names = {f for p in out for f in p.features}
        # NY-session bias should manifest as session_ny long, OR via the
        # downstream effect on returns appearing in trend / hour buckets.
        related = {"session_ny", "hour_q2", "hour_q3", "trend_up"}
        self.assertTrue(
            names & related,
            f"expected NY-related feature in survivors; got {names}",
        )
        # Every survivor's reported direction must match the train-side mean.
        for p in out:
            if p.direction == "long":
                self.assertGreater(p.train_mean_r, 0)
            else:
                self.assertLess(p.train_mean_r, 0)


if __name__ == "__main__":
    unittest.main()
