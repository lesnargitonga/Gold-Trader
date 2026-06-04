"""Tests for the risk hardening layer (D1 equity guard + D2 divergence guard)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gold_trader.infra import (
    DivergenceConfig,
    EquityGuardConfig,
    EventBus,
    EventKind,
    evaluate_divergence_guard,
    evaluate_equity_guard,
    evaluate_tick_age,
    flatten_account,
    open_state_db,
    trip_kill_switch,
)
from gold_trader.live.broker import (
    AccountInfo,
    BrokerError,
    ClosedTrade,
    OpenPosition,
    OrderSide,
    PendingOrder,
)


MAGIC = 20260507


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class _FakeBroker:
    name = "fake"

    def __init__(
        self,
        equity: float,
        balance: float,
        position: OpenPosition | None = None,
        pending: PendingOrder | None = None,
        deals: list[Any] | None = None,
    ) -> None:
        self._equity = equity
        self._balance = balance
        self._position = position
        self._pending = pending
        self._deals = deals or []
        self.cancelled: list[str] = []
        self.closed: list[str] = []
        self.fail_cancel = False
        self.fail_close = False

    def get_account_info(self) -> AccountInfo:
        return AccountInfo(
            equity=self._equity, balance=self._balance, currency="USD",
            margin_used=0.0, margin_free=self._equity, leverage=500.0,
        )

    def get_open_position(self, magic: int = MAGIC) -> OpenPosition | None:
        return self._position

    def get_pending_order(self, magic: int = MAGIC) -> PendingOrder | None:
        return self._pending

    def cancel_pending_order(self, broker_order_id: str) -> bool:
        if self.fail_cancel:
            raise BrokerError("simulated cancel failure")
        self.cancelled.append(broker_order_id)
        self._pending = None
        return True

    def close_position(self, broker_order_id: str, reason: str = "manual") -> ClosedTrade | None:
        if self.fail_close:
            raise BrokerError("simulated close failure")
        self.closed.append(broker_order_id)
        self._position = None
        return ClosedTrade(
            broker_order_id=broker_order_id, symbol="GOLD", side=OrderSide.BUY,
            units=0.01, entry_price=4700.0, exit_price=4690.0,
            opened_at=_utc(2026, 5, 7, 0), closed_at=_utc(2026, 5, 7, 1),
            pnl_dollars=-10.0, exit_reason=reason,
        )


def _seed_equity(db, ts: str, equity: float, balance: float) -> None:
    db.insert_equity_snapshot({
        "ts": ts, "broker_name": "fake", "equity": equity, "balance": balance,
        "margin_used": 0.0, "margin_free": equity, "paper_equity": None,
        "open_position_count": 0, "pending_order_count": 0,
    })


class EquityGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = open_state_db(Path(self.tmp.name) / "state.db")
        self.today = datetime.now(timezone.utc).date().isoformat()

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_ok_when_within_limits(self) -> None:
        _seed_equity(self.db, f"{self.today}T00:00:00+00:00", 10000.0, 10000.0)
        broker = _FakeBroker(equity=10010.0, balance=10010.0)
        v = evaluate_equity_guard(broker, self.db)
        self.assertEqual(v.decision, "ok")

    def test_trips_on_daily_drawdown(self) -> None:
        _seed_equity(self.db, f"{self.today}T00:00:00+00:00", 10000.0, 10000.0)
        # 5% loss > 4% limit
        broker = _FakeBroker(equity=9500.0, balance=10000.0)
        v = evaluate_equity_guard(broker, self.db)
        self.assertEqual(v.decision, "trip")
        self.assertEqual(v.triggered_rule, "daily_loss_fraction")

    def test_warns_at_80_percent(self) -> None:
        _seed_equity(self.db, f"{self.today}T00:00:00+00:00", 10000.0, 10000.0)
        # Seed a recent snapshot so the single-cycle rule does not trip first.
        _seed_equity(self.db, f"{self.today}T01:00:00+00:00", 9700.0, 10000.0)
        # 3.4% daily loss is > 80% of 4% but < 4%, and -0.4% vs prior is fine.
        broker = _FakeBroker(equity=9660.0, balance=10000.0)
        v = evaluate_equity_guard(broker, self.db)
        self.assertEqual(v.decision, "warn")

    def test_trips_on_absolute_floor(self) -> None:
        _seed_equity(self.db, f"{self.today}T00:00:00+00:00", 10000.0, 10000.0)
        broker = _FakeBroker(equity=400.0, balance=400.0)
        v = evaluate_equity_guard(
            broker, self.db,
            config=EquityGuardConfig(min_absolute_equity=500.0),
        )
        self.assertEqual(v.decision, "trip")
        self.assertEqual(v.triggered_rule, "min_absolute_equity")

    def test_trips_on_single_cycle_drop(self) -> None:
        _seed_equity(self.db, f"{self.today}T00:00:00+00:00", 10000.0, 10000.0)
        # Inject a recent prior snapshot with high equity.
        _seed_equity(self.db, f"{self.today}T01:00:00+00:00", 10100.0, 10000.0)
        # Drop 3% in one cycle (limit 2.5%)
        broker = _FakeBroker(equity=9797.0, balance=10000.0)
        v = evaluate_equity_guard(broker, self.db)
        self.assertEqual(v.decision, "trip")
        self.assertEqual(v.triggered_rule, "max_single_cycle_loss_fraction")


class FlattenAndKillSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = open_state_db(Path(self.tmp.name) / "state.db")
        self.bus = EventBus(self.db, jsonl_path=Path(self.tmp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _broker_with_open(self) -> _FakeBroker:
        pos = OpenPosition(
            broker_order_id="P1", symbol="GOLD", side=OrderSide.BUY, units=0.01,
            entry_price=4700.0, stop_price=4690.0, target_price=4720.0,
            opened_at=_utc(2026, 5, 7, 0), unrealised_pnl=-10.0, magic=MAGIC,
        )
        pend = PendingOrder(
            broker_order_id="O2", symbol="GOLD", side=OrderSide.BUY, units=0.01,
            entry_price=4710.0, stop_price=4700.0, target_price=4730.0,
            placed_at=_utc(2026, 5, 7, 0), magic=MAGIC,
        )
        return _FakeBroker(
            equity=9500.0, balance=10000.0, position=pos, pending=pend,
        )

    def test_flatten_cancels_and_closes(self) -> None:
        broker = self._broker_with_open()
        rep = flatten_account(broker, magic=MAGIC, reason="kill_switch")
        self.assertEqual(rep.cancelled_pending, ["O2"])
        self.assertEqual(rep.closed_positions, ["P1"])
        self.assertEqual(rep.errors, [])

    def test_flatten_collects_errors(self) -> None:
        broker = self._broker_with_open()
        broker.fail_close = True
        rep = flatten_account(broker, magic=MAGIC, reason="kill_switch")
        self.assertEqual(rep.cancelled_pending, ["O2"])
        self.assertEqual(rep.closed_positions, [])
        self.assertTrue(any("close_position" in e for e in rep.errors))

    def test_trip_kill_switch_emits_event_and_flattens(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        _seed_equity(self.db, f"{today}T00:00:00+00:00", 10000.0, 10000.0)
        broker = self._broker_with_open()
        verdict = evaluate_equity_guard(broker, self.db)
        self.assertEqual(verdict.decision, "trip")
        rep = trip_kill_switch(broker, self.db, self.bus, verdict, magic=MAGIC)
        self.assertEqual(rep.closed_positions, ["P1"])
        rows = self.db.query(
            "SELECT payload_json FROM events WHERE kind=?",
            (EventKind.KILL_SWITCH_TRIGGERED.value,),
        )
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["rule"], "daily_loss_fraction")
        self.assertEqual(payload["closed_positions"], ["P1"])
        self.assertFalse(payload["duplicate"])

    def test_trip_kill_switch_marks_duplicate_on_second_call(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        _seed_equity(self.db, f"{today}T00:00:00+00:00", 10000.0, 10000.0)
        broker = self._broker_with_open()
        verdict = evaluate_equity_guard(broker, self.db)
        trip_kill_switch(broker, self.db, self.bus, verdict, magic=MAGIC)
        # Second call: broker already flat but event still emitted as duplicate.
        verdict2 = evaluate_equity_guard(broker, self.db)
        # equity now 9500/10000 still trips daily limit
        trip_kill_switch(broker, self.db, self.bus, verdict2, magic=MAGIC)
        rows = self.db.query(
            "SELECT payload_json FROM events WHERE kind=? ORDER BY id",
            (EventKind.KILL_SWITCH_TRIGGERED.value,),
        )
        self.assertEqual(len(rows), 2)
        self.assertFalse(json.loads(rows[0]["payload_json"])["duplicate"])
        self.assertTrue(json.loads(rows[1]["payload_json"])["duplicate"])


class TickAgeTests(unittest.TestCase):
    def test_missing_when_none(self) -> None:
        v = evaluate_tick_age(None)
        self.assertEqual(v.decision, "missing")

    def test_ok_when_fresh(self) -> None:
        v = evaluate_tick_age(30.0, threshold_sec=300.0)
        self.assertEqual(v.decision, "ok")

    def test_stale_when_over_threshold(self) -> None:
        v = evaluate_tick_age(400.0, threshold_sec=300.0)
        self.assertEqual(v.decision, "stale")


class DivergenceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = open_state_db(Path(self.tmp.name) / "state.db")
        self.today = datetime.now(timezone.utc).date().isoformat()
        _seed_equity(
            self.db, f"{self.today}T00:00:00+00:00",
            equity=10000.0, balance=10000.0,
        )

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _seed_fill(self, deal_id: str, profit: float) -> None:
        self.db.upsert_fill({
            "deal_id": deal_id, "order_ticket": "O", "position_ticket": "P",
            "symbol": "GOLD", "side": "buy", "volume": 0.01, "price": 4700.0,
            "profit": profit, "swap": 0.0, "commission": 0.0, "fee": 0.0,
            "deal_type": 1, "entry_type": 1, "reason": 0,
            "time": f"{self.today}T01:00:00+00:00",
            "magic": MAGIC, "comment": "",
        })

    def test_ok_when_aligned(self) -> None:
        # Broker realised +9; ledger sums to +9; paper +9.
        self._seed_fill("D1", 9.0)
        broker = _FakeBroker(equity=10009.0, balance=10009.0)
        v = evaluate_divergence_guard(
            broker, self.db,
            paper_equity=10009.0, paper_starting_equity=10000.0, magic=MAGIC,
        )
        self.assertEqual(v.decision, "ok")

    def test_alert_on_broker_ledger_mismatch(self) -> None:
        # Broker says +50, ledger only has +9 → big drift.
        self._seed_fill("D1", 9.0)
        broker = _FakeBroker(equity=10050.0, balance=10050.0)
        v = evaluate_divergence_guard(
            broker, self.db,
            paper_equity=10009.0, paper_starting_equity=10000.0, magic=MAGIC,
        )
        self.assertEqual(v.decision, "alert")

    def test_warn_on_paper_drift_only(self) -> None:
        # Broker and ledger agree, paper sim wildly off.
        self._seed_fill("D1", 9.0)
        broker = _FakeBroker(equity=10009.0, balance=10009.0)
        v = evaluate_divergence_guard(
            broker, self.db,
            paper_equity=10500.0, paper_starting_equity=10000.0, magic=MAGIC,
        )
        self.assertEqual(v.decision, "warn")


if __name__ == "__main__":
    unittest.main()
