"""Tests for the concurrence-gated ensemble backtest engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence

import pytest

from gold_trader.backtest import (
    concurrence_at_bar,
    run_ensemble_backtest,
)
from gold_trader.backtest.ensemble_engine import _GatedStrategy, _index_signals
from gold_trader.models import BacktestConfig, MarketBar, Side, TradeSignal


# --------------------------------------------------------------------- fixtures
def _bar(ts: datetime, close: float = 2400.0) -> MarketBar:
    return MarketBar(
        timestamp=ts,
        open=close - 1.0,
        high=close + 1.0,
        low=close - 2.0,
        close=close,
        volume=100.0,
        spread=0.5,
        session="london",
        news_distance_minutes=120.0,
    )


def _make_bars(n: int = 200) -> list[MarketBar]:
    """Monotonically rising bars; large enough to cover any strategy warmup."""
    start = datetime(2024, 1, 8, 7, 0, tzinfo=timezone.utc)
    return [_bar(start + timedelta(minutes=15 * i), 2400.0 + i * 0.25)
            for i in range(n)]


class _FakeStrategy:
    """Minimal Strategy stand-in.  Fires LONG at the configured indices."""

    def __init__(self, name: str, fire_indices: Sequence[int],
                 *, side: Side = Side.LONG, score: float = 50.0,
                 warmup: int = 20) -> None:
        self.name = name
        self._fires = set(fire_indices)
        self._side = side
        self._score = score
        self._warmup = warmup

    def warmup_bars(self) -> int:
        return self._warmup

    def signal_for(self, bars, index):
        if index not in self._fires:
            return None
        bar = bars[index]
        ref = bar.close
        if self._side is Side.LONG:
            stop = ref - 2.0
            target = ref + 4.0
        else:
            stop = ref + 2.0
            target = ref - 4.0
        return TradeSignal(
            side=self._side,
            stop=stop,
            target=target,
            reason=f"{self.name}_fire",
            tags=(),
            risk_reward=2.0,
            size_multiplier=1.0,
            score=self._score,
        )


# ---------------------------------------------------- _index_signals semantics
class TestIndexSignals:
    def test_buckets_by_bar_and_side(self):
        bars = _make_bars(60)
        s_a = _FakeStrategy("a", [40], side=Side.LONG)
        s_b = _FakeStrategy("b", [40, 45], side=Side.LONG)
        s_c = _FakeStrategy("c", [40], side=Side.SHORT)
        idx, total, max_warmup = _index_signals(bars, [s_a, s_b, s_c])
        assert total == 4
        assert max_warmup == 20
        assert set(idx[40][Side.LONG]) == {("a", idx[40][Side.LONG][0][1]),
                                           ("b", idx[40][Side.LONG][1][1])}
        assert {nm for nm, _ in idx[40][Side.LONG]} == {"a", "b"}
        assert {nm for nm, _ in idx[40][Side.SHORT]} == {"c"}
        assert {nm for nm, _ in idx[45][Side.LONG]} == {"b"}

    def test_skips_last_bar(self):
        # Engine needs index+1 to fill, so signals on the last bar are useless.
        bars = _make_bars(40)
        s = _FakeStrategy("a", [38, 39], warmup=20)  # 39 is last_eligible-1
        idx, total, _ = _index_signals(bars, [s])
        # last_eligible = n_bars - 1 = 39, so the loop range is (warmup, 39)
        # → 38 included, 39 excluded
        assert 38 in idx
        assert 39 not in idx
        assert total == 1


# -------------------------------------------------- run_ensemble_backtest core
class TestRunEnsembleBacktest:
    def test_gate_blocks_low_concurrence(self):
        bars = _make_bars(80)
        # Only one strategy fires at index 40 → gate=2 should produce zero
        # gated events.
        s = _FakeStrategy("a", [40])
        cfg = BacktestConfig(starting_equity=10_000)
        r = run_ensemble_backtest(bars, [s], cfg, gate_min=2)
        assert r.n_signals_total == 1
        assert r.n_signals_gated_in == 0
        assert r.backtest.trades == ()

    def test_gate_admits_when_threshold_met(self):
        bars = _make_bars(80)
        s_a = _FakeStrategy("a", [40], side=Side.LONG)
        s_b = _FakeStrategy("b", [40], side=Side.LONG)
        cfg = BacktestConfig(starting_equity=10_000)
        r = run_ensemble_backtest(bars, [s_a, s_b], cfg, gate_min=2)
        assert r.n_signals_total == 2
        assert r.n_signals_gated_in == 1
        assert len(r.events) == 1
        ev = r.events[0]
        assert ev.bar_index == 40
        assert ev.side is Side.LONG
        assert set(ev.strategies) == {"a", "b"}

    def test_concurrence_is_per_side(self):
        # 3 LONG + 2 SHORT at the same bar must NOT cross-pollinate.
        bars = _make_bars(80)
        longs = [_FakeStrategy(f"l{i}", [40], side=Side.LONG) for i in range(3)]
        shorts = [_FakeStrategy(f"s{i}", [40], side=Side.SHORT) for i in range(2)]
        cfg = BacktestConfig(starting_equity=10_000)
        r = run_ensemble_backtest(bars, longs + shorts, cfg, gate_min=3)
        # Only the LONG side has 3 ≥ gate.
        assert r.n_signals_gated_in == 1
        assert r.events[0].side is Side.LONG

    def test_dedup_same_strategy_name(self):
        # Two instances sharing a name fire on the same bar/side: should
        # count as ONE distinct strategy, not two.
        bars = _make_bars(80)
        a1 = _FakeStrategy("a", [40], side=Side.LONG)
        a2 = _FakeStrategy("a", [40], side=Side.LONG)
        cfg = BacktestConfig(starting_equity=10_000)
        r = run_ensemble_backtest(bars, [a1, a2], cfg, gate_min=2)
        # Concurrence is 1 (one distinct name), so gate=2 must reject.
        assert r.n_signals_gated_in == 0

    def test_invalid_gate_min_raises(self):
        bars = _make_bars(40)
        with pytest.raises(ValueError):
            run_ensemble_backtest(bars, [_FakeStrategy("a", [25])],
                                  BacktestConfig(), gate_min=0)

    def test_weights_break_ties(self):
        # Three candidates, weights pick the highest-weighted one.
        bars = _make_bars(80)
        s_a = _FakeStrategy("a", [40], side=Side.LONG, score=20.0)
        s_b = _FakeStrategy("b", [40], side=Side.LONG, score=80.0)
        s_c = _FakeStrategy("c", [40], side=Side.LONG, score=50.0)
        cfg = BacktestConfig(starting_equity=10_000)
        r = run_ensemble_backtest(
            bars, [s_a, s_b, s_c], cfg, gate_min=3,
            weights={"a": 0.1, "b": 0.9, "c": 0.5},
        )
        assert r.events[0].chosen_strategy == "b"
        assert r.events[0].score == 80.0

    def test_alphabetical_fallback_no_weights(self):
        bars = _make_bars(80)
        s_z = _FakeStrategy("z", [40], side=Side.LONG, score=10.0)
        s_a = _FakeStrategy("a", [40], side=Side.LONG, score=99.0)
        cfg = BacktestConfig(starting_equity=10_000)
        r = run_ensemble_backtest(bars, [s_z, s_a], cfg, gate_min=2,
                                  weights=None)
        # Tie-break alphabetical → "a" wins.
        assert r.events[0].chosen_strategy == "a"


# ---------------------------------------------------------- concurrence_at_bar
class TestConcurrenceAtBar:
    def test_returns_per_side_lists(self):
        bars = _make_bars(80)
        s_a = _FakeStrategy("a", [50], side=Side.LONG)
        s_b = _FakeStrategy("b", [50], side=Side.LONG)
        s_c = _FakeStrategy("c", [50], side=Side.SHORT)
        result = concurrence_at_bar(bars, [s_a, s_b, s_c], 50)
        assert sorted(result[Side.LONG]) == ["a", "b"]
        assert result[Side.SHORT] == ["c"]

    def test_skips_warmup_bound(self):
        # Strategy warmup=100 cannot be queried at index=50.
        bars = _make_bars(80)
        s = _FakeStrategy("late", [50], warmup=100)
        result = concurrence_at_bar(bars, [s], 50)
        assert result[Side.LONG] == []
        assert result[Side.SHORT] == []

    def test_dedupes_duplicate_names(self):
        bars = _make_bars(80)
        a1 = _FakeStrategy("a", [50], side=Side.LONG)
        a2 = _FakeStrategy("a", [50], side=Side.LONG)
        result = concurrence_at_bar(bars, [a1, a2], 50)
        assert result[Side.LONG] == ["a"]


# -------------------------------------------------------- _GatedStrategy proto
class TestGatedStrategyVirtual:
    def test_emits_only_mapped_indices(self):
        bars = _make_bars(80)
        # Build a fake signal map at index 40 only.
        ts = TradeSignal(
            side=Side.LONG,
            stop=bars[40].close - 2.0,
            target=bars[40].close + 4.0,
            reason="probe",
            tags=(),
            risk_reward=2.0,
        )
        gs = _GatedStrategy(signal_map={40: ts}, warmup=20)
        assert gs.warmup_bars() == 20
        assert gs.signal_for(bars, 40) is ts
        assert gs.signal_for(bars, 41) is None
        assert gs.signal_for(bars, 0) is None
