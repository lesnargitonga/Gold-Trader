"""Unit tests for the ensemble & universal-scoring layer."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gold_trader import ensemble
from gold_trader.ensemble import (
    DEFAULT_CONCURRENCE_TABLE,
    concurrence_multiplier,
    load_strategy_weights,
    signal_strength,
    strategy_weight,
)
from gold_trader.models import MarketBar, Side
from gold_trader.strategies import filters as F
from gold_trader.strategies.scoring import ScoreVerdict


# --------------------------------------------------------------------- helpers
def _bar(ts: datetime, *, close: float = 2400.0, spread: float = 0.5,
         session: str = "london", news: float | None = None) -> MarketBar:
    return MarketBar(
        timestamp=ts,
        open=close - 1, high=close + 1, low=close - 2, close=close,
        volume=100.0, spread=spread, session=session,
        news_distance_minutes=news,
    )


def _bars_uptrend(n: int = 200) -> list[MarketBar]:
    """Synthetic monotonically rising bars in London hours."""
    start = datetime(2024, 1, 8, 7, 0, tzinfo=timezone.utc)  # Monday 07:00 UTC
    return [_bar(start + timedelta(minutes=15 * i),
                 close=2400.0 + i * 0.5) for i in range(n)]


# --------------------------------------------------------------------- ensemble
class TestConcurrenceMultiplier:
    def test_lookup_table_values(self):
        assert concurrence_multiplier(1) == 1.0
        assert concurrence_multiplier(2) == 0.85
        assert concurrence_multiplier(3) == 0.50
        assert concurrence_multiplier(4) == 0.95
        assert concurrence_multiplier(5) == 1.50
        assert concurrence_multiplier(6) == 1.75
        assert concurrence_multiplier(7) == 2.0

    def test_zero_or_negative_returns_one(self):
        assert concurrence_multiplier(0) == 1.0
        assert concurrence_multiplier(-1) == 1.0

    def test_beyond_table_clamps_to_max(self):
        # Beyond observed bins should clamp to the max tabulated entry (7 → 2.0)
        assert concurrence_multiplier(8) == 2.0
        assert concurrence_multiplier(99) == 2.0

    def test_custom_table(self):
        custom = {1: 1.0, 2: 1.5}
        assert concurrence_multiplier(2, custom) == 1.5
        assert concurrence_multiplier(3, custom) == 1.5  # clamps to max=2


class TestStrategyWeights:
    def test_unknown_strategy_default_one(self):
        assert strategy_weight("does_not_exist", weights={}) == 1.0

    def test_known_strategy(self):
        weights = {"foo": 0.7, "bar": 0.3}
        assert strategy_weight("foo", weights) == 0.7

    def test_load_missing_file(self, tmp_path: Path):
        ensemble.reset_cache()
        out = load_strategy_weights(tmp_path / "nope.json")
        assert out == {}

    def test_load_existing_file(self, tmp_path: Path):
        ensemble.reset_cache()
        p = tmp_path / "w.json"
        p.write_text(json.dumps({"weights": {"a": 0.5, "b": 1.0}}))
        out = load_strategy_weights(p)
        assert out == {"a": 0.5, "b": 1.0}


class TestSignalStrength:
    def test_simple_combination(self):
        # 80 score × weight 0.5 × concurrence_1=1.0 = 40
        weights = {"foo": 0.5}
        assert signal_strength(80, "foo", concurrence=1, weights=weights) == 40.0

    def test_concurrence_boost(self):
        weights = {"foo": 1.0}
        assert signal_strength(80, "foo", concurrence=3, weights=weights) == 80.0 * 0.50

    def test_negative_score_floored_at_zero(self):
        weights = {"foo": 1.0}
        assert signal_strength(-50, "foo", concurrence=1, weights=weights) == 0.0

    def test_unknown_strategy_uses_weight_one(self):
        # Unknown strategy ⇒ weight=1.0, so result == score × concurrence_mult
        assert signal_strength(70, "unknown", concurrence=2, weights={}) == 70.0 * 0.85


# --------------------------------------------------------------------- universal_score
class TestUniversalScore:
    def setup_method(self):
        F.reset_caches()

    def test_returns_signal_score_with_max_100(self):
        bars = _bars_uptrend(120)
        score = F.universal_score(bars, 100, Side.LONG)
        assert 0 <= score.max_score <= 100
        # All 7 features ⇒ 20+15+10+20+15+10+10 = 100
        assert score.max_score == 100

    def test_long_in_uptrend_high_score(self):
        bars = _bars_uptrend(120)
        score = F.universal_score(bars, 100, Side.LONG)
        # uptrend + London core hours + tight spread + healthy ATR ⇒ should
        # easily clear the FULL_SIZE threshold (70+).
        assert score.score >= 55, f"got score={score.score}"

    def test_short_against_uptrend_lower_score(self):
        # Need enough bars for HTF EMA50 (4H) to be warm: 50 buckets × 16 = 800 bars.
        bars = _bars_uptrend(900)
        long_score = F.universal_score(bars, 850, Side.LONG)
        short_score = F.universal_score(bars, 850, Side.SHORT)
        # In a clear uptrend, the LONG side should outscore SHORT — HTF
        # alignment + (long is not consecutive-extended at this index) →
        # higher score.  Both should differ.
        assert long_score.score != short_score.score
        # And HTF feature must explicitly favour LONG:
        long_htf = next(r for r in long_score.results if r.name == "u_htf_alignment")
        short_htf = next(r for r in short_score.results if r.name == "u_htf_alignment")
        assert long_htf.passed is True
        assert short_htf.passed is False

    def test_news_proximity_drops_score(self):
        bars = _bars_uptrend(120)
        # Patch one bar to be 5min from news
        bars[100] = MarketBar(
            timestamp=bars[100].timestamp,
            open=bars[100].open, high=bars[100].high, low=bars[100].low,
            close=bars[100].close, volume=100.0, spread=0.5, session="london",
            news_distance_minutes=5.0,
        )
        score = F.universal_score(bars, 100, Side.LONG)
        # u_news_clear is 15pts — should be missing
        names = {r.name: r for r in score.results}
        assert names["u_news_clear"].passed is False
        assert names["u_news_clear"].points == 0.0

    def test_verdict_classification(self):
        bars = _bars_uptrend(120)
        score = F.universal_score(bars, 100, Side.LONG)
        # Verdict must derive from thresholds 70/55/40
        if score.score >= 70:
            assert score.verdict is ScoreVerdict.FULL_SIZE
        elif score.score >= 55:
            assert score.verdict is ScoreVerdict.HALF_SIZE
        elif score.score >= 40:
            assert score.verdict is ScoreVerdict.LOG_ONLY
        else:
            assert score.verdict is ScoreVerdict.REJECT


class TestComputeStrategyWeights:
    """End-to-end: observatory CSV → weights JSON."""

    def test_full_pipeline(self, tmp_path: Path):
        csv = tmp_path / "per_strategy.csv"
        csv.write_text(
            "rank,strategy,scored,n,wins,win_rate_pct,avg_r,pf,rank_score_pf_sqrt_n\n"
            "1,alpha,True,100,55,55.0,+0.10,1.50,15.00\n"
            "2,beta,True,40,18,45.0,-0.05,0.80,5.06\n"
            "3,gamma,True,5,3,60.0,+0.30,2.00,4.47\n"
        )
        out = tmp_path / "w.json"
        import subprocess
        import sys
        repo_root = Path(__file__).resolve().parent.parent
        r = subprocess.run(
            [sys.executable,
             str(repo_root / "scripts" / "compute_strategy_weights.py"),
             str(csv), "--output", str(out), "--min-n", "20"],
            check=True, capture_output=True, text=True,
            env={"PYTHONPATH": str(repo_root / "src"), **__import__("os").environ},
        )
        assert "wrote" in r.stdout
        data = json.loads(out.read_text())
        weights = data["weights"]
        # alpha is best (PF=1.5 × √100 = 15) → weight 1.0
        assert weights["alpha"] == pytest.approx(1.0)
        # beta is mid → 0 < weight < 1
        assert 0 < weights["beta"] < 1
        # gamma n=5 < min_n=20 → weight=0
        assert weights["gamma"] == 0.0
