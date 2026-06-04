"""Tests for the broker abstraction and paper broker."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from gold_trader.live import (
    AccountInfo,
    Broker,
    BrokerError,
    OrderRequest,
    OrderSide,
    PaperBroker,
    get_broker_from_env,
)
from gold_trader.paper.state import (
    PaperPosition,
    PaperState,
    save_paper_state,
)


def _empty_state() -> PaperState:
    return PaperState(
        open_position=None,
        closed_positions=[],
        paper_equity=10000.0,
        daily_peak_equity=10000.0,
        last_updated="2026-05-07T00:00:00+00:00",
        total_trades=0,
        winning_trades=0,
    )


def _state_with_open() -> PaperState:
    pos = PaperPosition(
        opened_at="2026-05-07T17:11:48+00:00",
        family="asian_range_breakout",
        timeframe_minutes=60,
        side="long",
        entry=4746.36,
        stop=4685.32,
        target=4843.04,
    )
    return PaperState(
        open_position=pos,
        closed_positions=[],
        paper_equity=10000.0,
        daily_peak_equity=10000.0,
        last_updated="2026-05-07T17:11:48+00:00",
        total_trades=0,
        winning_trades=0,
    )


class PaperBrokerTests(unittest.TestCase):
    def test_account_info_when_flat(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            save_paper_state(_empty_state(), path)
            broker = PaperBroker(path)
            info = broker.get_account_info()
            self.assertIsInstance(info, AccountInfo)
            self.assertEqual(info.equity, 10000.0)
            self.assertEqual(info.currency, "USD")

    def test_account_info_creates_default_when_no_state(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "missing.json"  # never created
            broker = PaperBroker(path, starting_equity=5000.0)
            info = broker.get_account_info()
            self.assertEqual(info.equity, 5000.0)

    def test_get_open_position_when_flat(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            save_paper_state(_empty_state(), path)
            broker = PaperBroker(path)
            self.assertIsNone(broker.get_open_position())

    def test_get_open_position_returns_correct_fields(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            save_paper_state(_state_with_open(), path)
            broker = PaperBroker(path)
            op = broker.get_open_position()
            self.assertIsNotNone(op)
            assert op is not None
            self.assertEqual(op.side, OrderSide.BUY)
            self.assertAlmostEqual(op.entry_price, 4746.36)
            self.assertAlmostEqual(op.stop_price, 4685.32)
            self.assertAlmostEqual(op.target_price, 4843.04)
            self.assertEqual(op.symbol, "XAUUSD")

    def test_place_market_order_rejects_when_position_open(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            save_paper_state(_state_with_open(), path)
            broker = PaperBroker(path)
            req = OrderRequest(
                symbol="XAUUSD",
                side=OrderSide.BUY,
                risk_dollars=100.0,
                stop_price=4700.0,
                target_price=4800.0,
            )
            result = broker.place_market_order(req)
            self.assertFalse(result.accepted)
            self.assertIn("already open", result.error or "")

    def test_place_market_order_when_flat_raises_brokererror(self) -> None:
        """PaperBroker is read-only for placement; entries flow through paper.state."""
        with TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            save_paper_state(_empty_state(), path)
            broker = PaperBroker(path)
            req = OrderRequest(
                symbol="XAUUSD",
                side=OrderSide.BUY,
                risk_dollars=100.0,
                stop_price=4700.0,
                target_price=4800.0,
            )
            with self.assertRaises(BrokerError):
                broker.place_market_order(req)

    def test_close_position_when_flat_returns_none(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            save_paper_state(_empty_state(), path)
            broker = PaperBroker(path)
            self.assertIsNone(broker.close_position("anything"))

    def test_close_position_clears_open_state(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            save_paper_state(_state_with_open(), path)
            broker = PaperBroker(path)
            trade = broker.close_position("paper:test", reason="manual")
            self.assertIsNotNone(trade)
            assert trade is not None
            self.assertEqual(trade.exit_reason, "manual")
            # State should now be flat.
            self.assertIsNone(broker.get_open_position())


class GetBrokerFromEnvTests(unittest.TestCase):
    def test_default_returns_paper(self) -> None:
        # Ensure GOLD_BROKER is unset.
        prev = os.environ.pop("GOLD_BROKER", None)
        try:
            broker = get_broker_from_env()
            self.assertEqual(broker.name, "paper")
            self.assertIsInstance(broker, PaperBroker)
        finally:
            if prev is not None:
                os.environ["GOLD_BROKER"] = prev

    def test_explicit_paper(self) -> None:
        prev = os.environ.get("GOLD_BROKER")
        os.environ["GOLD_BROKER"] = "paper"
        try:
            broker = get_broker_from_env()
            self.assertEqual(broker.name, "paper")
        finally:
            if prev is None:
                del os.environ["GOLD_BROKER"]
            else:
                os.environ["GOLD_BROKER"] = prev

    def test_unknown_raises(self) -> None:
        prev = os.environ.get("GOLD_BROKER")
        os.environ["GOLD_BROKER"] = "bogus"
        try:
            with self.assertRaises(BrokerError):
                get_broker_from_env()
        finally:
            if prev is None:
                del os.environ["GOLD_BROKER"]
            else:
                os.environ["GOLD_BROKER"] = prev

    def test_mt5_local_constructs_without_connecting(self) -> None:
        """mt5_local should be constructable on Linux; connect() is what fails."""
        prev = os.environ.get("GOLD_BROKER")
        os.environ["GOLD_BROKER"] = "mt5_local"
        try:
            broker = get_broker_from_env()
            self.assertEqual(broker.name, "mt5_local")
        finally:
            if prev is None:
                del os.environ["GOLD_BROKER"]
            else:
                os.environ["GOLD_BROKER"] = prev

    def test_mt5_remote_constructs_without_connecting(self) -> None:
        prev = os.environ.get("GOLD_BROKER")
        os.environ["GOLD_BROKER"] = "mt5_remote"
        try:
            broker = get_broker_from_env()
            self.assertEqual(broker.name, "mt5_remote")
        finally:
            if prev is None:
                del os.environ["GOLD_BROKER"]
            else:
                os.environ["GOLD_BROKER"] = prev


class ProtocolConformanceTests(unittest.TestCase):
    def test_paper_broker_satisfies_broker_protocol(self) -> None:
        with TemporaryDirectory() as td:
            broker: Broker = PaperBroker(Path(td) / "state.json")
            # Static-typed assignment above is the real check; the runtime
            # call below ensures the methods actually exist.
            self.assertTrue(hasattr(broker, "get_account_info"))
            self.assertTrue(hasattr(broker, "get_open_position"))
            self.assertTrue(hasattr(broker, "place_market_order"))
            self.assertTrue(hasattr(broker, "close_position"))


if __name__ == "__main__":
    unittest.main()
