"""Tests for the production-grade infrastructure layer."""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gold_trader.infra import (
    EventBus,
    EventKind,
    JsonFormatter,
    configure_logging,
    get_logger,
    open_state_db,
    sync_fills_ledger,
)
from gold_trader.live.broker import Deal, OrderSide


class StructuredLoggingTests(unittest.TestCase):
    def test_json_formatter_includes_extras(self) -> None:
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="thing_happened", args=(), exc_info=None,
        )
        record.broker = "mt5_remote"
        record.equity = 10000.0
        out = JsonFormatter().format(record)
        decoded = json.loads(out)
        self.assertEqual(decoded["event"], "thing_happened")
        self.assertEqual(decoded["broker"], "mt5_remote")
        self.assertEqual(decoded["equity"], 10000.0)
        self.assertEqual(decoded["level"], "INFO")
        # Timestamp parses.
        datetime.fromisoformat(decoded["ts"])

    def test_configure_creates_rotating_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = configure_logging(
                log_dir=Path(tmp),
                level="DEBUG",
                console=False,
                log_filename="t.jsonl",
            )
            log = get_logger("gold_trader.test")
            log.info("hello", extra={"k": "v"})
            for h in logging.getLogger().handlers:
                h.flush()
            self.assertTrue(path.exists())
            line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
            decoded = json.loads(line)
            self.assertEqual(decoded["event"], "hello")
            self.assertEqual(decoded["k"], "v")


class StateDBTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = open_state_db(Path(self.tmp.name) / "state.db")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_schema_version_set(self) -> None:
        row = self.db.query_one("PRAGMA user_version")
        self.assertEqual(int(list(row)[0]), 1)

    def test_upsert_position_idempotent(self) -> None:
        pos = {
            "ticket": "111", "symbol": "GOLD", "side": "buy", "units": 0.01,
            "entry_price": 4700.0, "stop_price": 4690.0, "target_price": 4720.0,
            "opened_at": "2026-05-07T00:00:00+00:00",
            "closed_at": None, "closed_price": None, "pnl_dollars": None,
            "exit_reason": None, "magic": 20260507,
            "family": "asian_range_breakout", "timeframe_minutes": 60,
            "status": "open",
        }
        self.db.upsert_position(pos)
        self.db.upsert_position({**pos, "status": "closed", "pnl_dollars": 9.0})
        rows = self.db.query("SELECT * FROM positions WHERE ticket=?", ("111",))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "closed")
        self.assertAlmostEqual(rows[0]["pnl_dollars"], 9.0)

    def test_upsert_fill_returns_false_on_duplicate(self) -> None:
        fill = {
            "deal_id": "D1", "order_ticket": "O1", "position_ticket": "P1",
            "symbol": "GOLD", "side": "buy", "volume": 0.01, "price": 4700.0,
            "profit": 0.0, "swap": 0.0, "commission": 0.0, "fee": 0.0,
            "deal_type": 0, "entry_type": 0, "reason": 0,
            "time": "2026-05-07T00:00:00+00:00", "magic": 20260507, "comment": "",
        }
        self.assertTrue(self.db.upsert_fill(fill))
        self.assertFalse(self.db.upsert_fill(fill))

    def test_transaction_rollback(self) -> None:
        try:
            with self.db.transaction() as con:
                con.execute(
                    "INSERT INTO equity_snapshots (ts, broker_name, equity, balance) "
                    "VALUES (?, ?, ?, ?)",
                    ("2026-05-07T00:00:00+00:00", "fake", 100.0, 100.0),
                )
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        rows = self.db.query("SELECT COUNT(*) AS c FROM equity_snapshots")
        self.assertEqual(rows[0]["c"], 0)


class EventBusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = open_state_db(Path(self.tmp.name) / "state.db")
        self.jsonl = Path(self.tmp.name) / "events.jsonl"
        self.bus = EventBus(self.db, jsonl_path=self.jsonl)

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_publish_persists_to_db_and_jsonl(self) -> None:
        ev = self.bus.publish(EventKind.ORDER_PLACED, {"ticket": "42"})
        self.assertEqual(ev.kind, EventKind.ORDER_PLACED)
        rows = self.db.query("SELECT kind, payload_json FROM events")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "order_placed")
        self.assertEqual(json.loads(rows[0]["payload_json"]), {"ticket": "42"})
        # JSONL mirror.
        line = self.jsonl.read_text(encoding="utf-8").strip().splitlines()[0]
        decoded = json.loads(line)
        self.assertEqual(decoded["kind"], "order_placed")
        self.assertEqual(decoded["payload"], {"ticket": "42"})

    def test_subscribers_invoked(self) -> None:
        seen: list[EventKind] = []
        self.bus.subscribe(lambda e: seen.append(e.kind))
        self.bus.publish(EventKind.SIGNAL_EMITTED, {"family": "x"})
        self.bus.publish(EventKind.DECISION_MADE, {})
        self.assertEqual(
            seen, [EventKind.SIGNAL_EMITTED, EventKind.DECISION_MADE],
        )

    def test_bad_subscriber_does_not_break_publish(self) -> None:
        self.bus.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("nope")))
        ok_called = []
        self.bus.subscribe(lambda e: ok_called.append(e.kind))
        # Should not raise even though the first subscriber throws.
        self.bus.publish(EventKind.AGENT_CYCLE_STARTED, {})
        self.assertEqual(ok_called, [EventKind.AGENT_CYCLE_STARTED])

    def test_correlation_id_round_trip(self) -> None:
        cid = self.bus.new_correlation_id()
        self.bus.publish(EventKind.SIGNAL_EMITTED, {}, correlation_id=cid)
        self.bus.publish(EventKind.DECISION_MADE, {}, correlation_id=cid)
        rows = self.db.query(
            "SELECT kind FROM events WHERE correlation_id=? ORDER BY id", (cid,),
        )
        self.assertEqual([r["kind"] for r in rows], ["signal_emitted", "decision_made"])


class _FakeBroker:
    def __init__(self, deals: list[Deal]) -> None:
        self._deals = deals
        self.calls: list[datetime] = []

    def get_deals_since(self, since: datetime, magic: int = 20260507) -> list[Deal]:
        self.calls.append(since)
        return [d for d in self._deals if d.time >= since and d.magic == magic]


class FillsLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = open_state_db(Path(self.tmp.name) / "state.db")
        self.bus = EventBus(self.db, jsonl_path=Path(self.tmp.name) / "events.jsonl")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def _deal(
        self, deal_id: str, position_ticket: str, entry_type: int,
        side: OrderSide, profit: float, t: datetime,
    ) -> Deal:
        return Deal(
            deal_id=deal_id, order_ticket="O" + deal_id,
            position_ticket=position_ticket, symbol="GOLD", side=side,
            volume=0.01, price=4700.0, profit=profit, swap=0.0,
            commission=0.0, fee=0.0, deal_type=0 if side is OrderSide.BUY else 1,
            entry_type=entry_type, reason=0, time=t, magic=20260507, comment="",
        )

    def test_dedupes_and_emits_on_first_pull(self) -> None:
        t0 = datetime.now(timezone.utc) - timedelta(hours=1)
        d1 = self._deal("1", "P1", 0, OrderSide.BUY, 0.0, t0)  # entry IN
        d2 = self._deal("2", "P1", 1, OrderSide.SELL, 9.0, t0 + timedelta(minutes=30))  # OUT
        broker = _FakeBroker([d1, d2])
        # Seed positions table so the round-trip update finds the row.
        self.db.upsert_position({
            "ticket": "P1", "symbol": "GOLD", "side": "buy", "units": 0.01,
            "entry_price": 4700.0, "stop_price": 4690.0, "target_price": 4720.0,
            "opened_at": t0.isoformat(), "closed_at": None, "closed_price": None,
            "pnl_dollars": None, "exit_reason": None, "magic": 20260507,
            "family": None, "timeframe_minutes": None, "status": "open",
        })

        result = sync_fills_ledger(broker, self.db, self.bus, magic=20260507)
        self.assertEqual(result["new_deals"], 2)
        self.assertEqual(result["new_round_trips"], 1)

        # Run again — should be a no-op (idempotent) thanks to watermark + dedupe.
        result2 = sync_fills_ledger(broker, self.db, self.bus, magic=20260507)
        self.assertEqual(result2["new_deals"], 0)
        self.assertEqual(result2["new_round_trips"], 0)

        # POSITION_CLOSED emitted with realised pnl.
        rows = self.db.query(
            "SELECT payload_json FROM events WHERE kind='position_closed'"
        )
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload_json"])
        self.assertEqual(payload["position_ticket"], "P1")
        self.assertAlmostEqual(payload["pnl_dollars"], 9.0)

        # Position row marked closed.
        pos = self.db.query_one("SELECT status, pnl_dollars FROM positions WHERE ticket='P1'")
        self.assertEqual(pos["status"], "closed")
        self.assertAlmostEqual(pos["pnl_dollars"], 9.0)


if __name__ == "__main__":
    unittest.main()
