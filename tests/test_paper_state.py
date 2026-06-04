from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gold_trader.models import Side
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.paper import (
    load_paper_state,
    monitor_open_position,
    open_position_from_decision,
    save_paper_state,
)
from gold_trader.research.state import DecisionPlan


def _make_accept_decision(side: str = "long") -> DecisionPlan:
    return DecisionPlan(
        status="accept",
        family="liquidity_sweep",
        timeframe_minutes=15,
        side=Side.LONG if side == "long" else Side.SHORT,
        reference_price=2000.0,
        stop=1990.0,
        target=2020.0,
        score=7,
        risk_reward=2.0,
        rationale=("synthetic test decision",),
    )


class PaperStateLoadSaveTests(unittest.TestCase):
    def test_fresh_state_created_when_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper_state.json"
            state = load_paper_state(path, starting_equity=5000.0)
            self.assertAlmostEqual(state.paper_equity, 5000.0)
            self.assertIsNone(state.open_position)
            self.assertEqual(state.total_trades, 0)

    def test_round_trip_save_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper_state.json"
            state = load_paper_state(path, starting_equity=10_000.0)
            state.paper_equity = 9800.0
            state.total_trades = 3
            state.winning_trades = 2
            save_paper_state(state, path)

            state2 = load_paper_state(path)
            self.assertAlmostEqual(state2.paper_equity, 9800.0)
            self.assertEqual(state2.total_trades, 3)
            self.assertEqual(state2.winning_trades, 2)


class PaperStatePositionTests(unittest.TestCase):
    def test_open_position_from_accept_decision(self) -> None:
        bars = generate_synthetic_bars(count=100, seed=1)
        decision = _make_accept_decision(side="long")
        pos = open_position_from_decision(decision, bars)
        self.assertIsNotNone(pos)
        self.assertEqual(pos.side, "long")
        self.assertEqual(pos.family, "liquidity_sweep")
        self.assertEqual(pos.status, "open")

    def test_open_position_from_hold_returns_none(self) -> None:
        bars = generate_synthetic_bars(count=100, seed=2)
        decision = DecisionPlan(
            status="hold",
            family=None,
            timeframe_minutes=None,
            side=None,
            reference_price=None,
            stop=None,
            target=None,
            score=0,
            risk_reward=0.0,
            rationale=("hold",),
        )
        pos = open_position_from_decision(decision, bars)
        self.assertIsNone(pos)


class PaperStateMonitorTests(unittest.TestCase):
    def test_kill_switch_triggered_at_96pct(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper_state.json"
            state = load_paper_state(path, starting_equity=10_000.0)
            state.daily_peak_equity = 10_000.0
            state.paper_equity = 9_550.0  # below 96%
            self.assertTrue(state.kill_switch_triggered)

    def test_kill_switch_not_triggered_when_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper_state.json"
            state = load_paper_state(path, starting_equity=10_000.0)
            state.daily_peak_equity = 10_000.0
            state.paper_equity = 9_700.0  # above 96%
            self.assertFalse(state.kill_switch_triggered)

    def test_monitor_open_position_no_event_when_price_between_levels(self) -> None:
        """Position stays open when all bars are between stop and target."""
        bars = generate_synthetic_bars(count=50, seed=7)
        avg_price = sum(b.close for b in bars) / len(bars)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper_state.json"
            state = load_paper_state(path, starting_equity=10_000.0)
            decision = DecisionPlan(
                status="accept",
                family="liquidity_sweep",
                timeframe_minutes=15,
                side=Side.LONG,
                reference_price=avg_price,
                stop=avg_price - 1000,  # far away down
                target=avg_price + 1000,  # far away up
                score=7,
                risk_reward=2.0,
                rationale=("test",),
            )
            pos = open_position_from_decision(decision, bars)
            if pos is None:
                self.skipTest("open_position_from_decision returned None for this price level")
            state.open_position = pos
            _, event = monitor_open_position(state, bars)
            # With stop far below and target far above, no close event expected
            self.assertIsNone(event)


class PaperStateDailyResetTests(unittest.TestCase):
    def test_daily_reset_if_needed_changes_date(self) -> None:
        state = load_paper_state(Path("/nonexistent"), starting_equity=10_000.0)
        state.daily_reset_date = "2020-01-01"  # clearly in the past
        state.daily_peak_equity = 9000.0
        state.daily_trades_opened = 5
        new_state = state.with_daily_reset_if_needed()
        # Resets: peak becomes current equity, trades reset to 0, date = today
        self.assertAlmostEqual(new_state.daily_peak_equity, state.paper_equity)
        self.assertEqual(new_state.daily_trades_opened, 0)
        self.assertNotEqual(new_state.daily_reset_date, "2020-01-01")

    def test_daily_reset_not_triggered_when_same_day(self) -> None:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        state = load_paper_state(Path("/nonexistent"), starting_equity=10_000.0)
        state.daily_reset_date = today
        state.daily_peak_equity = 9500.0
        state.daily_trades_opened = 2
        new_state = state.with_daily_reset_if_needed()
        # No reset: same object returned (or identical values)
        self.assertIs(new_state, state)
        self.assertAlmostEqual(new_state.daily_peak_equity, 9500.0)
        self.assertEqual(new_state.daily_trades_opened, 2)

    def test_daily_trades_opened_persists_through_save_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper_state.json"
            state = load_paper_state(path, starting_equity=10_000.0)
            state.daily_trades_opened = 3
            state.daily_reset_date = "2025-01-01"
            save_paper_state(state, path)
            state2 = load_paper_state(path)
            self.assertEqual(state2.daily_trades_opened, 3)
            self.assertEqual(state2.daily_reset_date, "2025-01-01")

    def test_monitor_position_preserves_daily_trades_count(self) -> None:
        """Closing a position should not reset daily_trades_opened."""
        bars = generate_synthetic_bars(count=50, seed=7)
        avg_price = sum(b.close for b in bars) / len(bars)
        state = load_paper_state(Path("/nonexistent"), starting_equity=10_000.0)
        state.daily_trades_opened = 2
        decision = DecisionPlan(
            status="accept",
            family="liquidity_sweep",
            timeframe_minutes=15,
            side=Side.LONG,
            reference_price=avg_price,
            stop=avg_price - 0.001,   # stop very close → will hit immediately
            target=avg_price + 2.0,
            score=7,
            risk_reward=2.0,
            rationale=("test",),
        )
        pos = open_position_from_decision(decision, bars)
        if pos is None:
            self.skipTest("open_position_from_decision returned None")
        state.open_position = pos
        state.last_updated = "2000-01-01T00:00:00+00:00"  # force bars to be "after" open
        new_state, _event = monitor_open_position(state, bars)
        self.assertEqual(new_state.daily_trades_opened, 2)


if __name__ == "__main__":
    unittest.main()
