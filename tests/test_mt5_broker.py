"""Tests for MT5LocalBroker using an injected fake MetaTrader5 module."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from gold_trader.live import MT5LocalBroker
from gold_trader.live.broker import (
    BrokerError,
    OrderRequest,
    OrderSide,
)


# ---------------------------------------------------------------------------
# Fake MetaTrader5 module — covers just the surface the broker uses.
# ---------------------------------------------------------------------------
class FakeMT5:
    # Action / order / position constants
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 7
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    RES_S_OK = 1

    def __init__(
        self,
        *,
        symbol: str = "GOLD",
        bid: float = 4700.0,
        ask: float = 4700.50,
        contract_size: float = 100.0,
        volume_min: float = 0.01,
        volume_max: float = 100.0,
        volume_step: float = 0.01,
        point: float = 0.01,
        digits: int = 2,
        trade_stops_level: int = 0,
        equity: float = 10000.0,
        balance: float = 10000.0,
        currency: str = "USD",
        leverage: float = 100.0,
        positions: list[Any] | None = None,
    ) -> None:
        self._symbol = symbol
        self._bid = bid
        self._ask = ask
        self._contract_size = contract_size
        self._volume_min = volume_min
        self._volume_max = volume_max
        self._volume_step = volume_step
        self._point = point
        self._digits = digits
        self._trade_stops_level = trade_stops_level
        self._equity = equity
        self._balance = balance
        self._currency = currency
        self._leverage = leverage
        self._positions = list(positions or [])
        self._pending: list[Any] = []
        self._initialised = False
        self.sent_orders: list[dict[str, Any]] = []
        self._next_ticket = 1000
        self._last_error: tuple[int, str] = (1, "ok")

    # --- lifecycle ---
    def initialize(self, **kwargs: Any) -> bool:
        self._initialised = True
        return True

    def shutdown(self) -> None:
        self._initialised = False

    def last_error(self) -> tuple[int, str]:
        return self._last_error

    # --- symbol ---
    def symbol_select(self, symbol: str, enable: bool) -> bool:
        return symbol == self._symbol

    def symbol_info(self, symbol: str) -> Any:
        if symbol != self._symbol:
            return None
        return SimpleNamespace(
            name=symbol,
            trade_contract_size=self._contract_size,
            volume_min=self._volume_min,
            volume_max=self._volume_max,
            volume_step=self._volume_step,
            point=self._point,
            digits=self._digits,
            trade_stops_level=self._trade_stops_level,
            trade_freeze_level=0,
        )

    def symbol_info_tick(self, symbol: str) -> Any:
        if symbol != self._symbol:
            return None
        return SimpleNamespace(bid=self._bid, ask=self._ask, time=int(datetime.now(timezone.utc).timestamp()))

    # --- account ---
    def account_info(self) -> Any:
        return SimpleNamespace(
            equity=self._equity,
            balance=self._balance,
            currency=self._currency,
            margin=0.0,
            margin_free=self._equity,
            leverage=self._leverage,
        )

    # --- positions ---
    def positions_get(self, symbol: str | None = None) -> tuple[Any, ...]:
        return tuple(self._positions)

    # --- pending orders ---
    def orders_get(self, symbol: str | None = None) -> tuple[Any, ...]:
        return tuple(self._pending)

    # --- orders ---
    def order_send(self, request: dict[str, Any]) -> Any:
        self.sent_orders.append(dict(request))
        action = request.get("action")
        if action == self.TRADE_ACTION_REMOVE:
            order_id = int(request["order"])
            self._pending = [p for p in self._pending if int(p.ticket) != order_id]
            return SimpleNamespace(
                retcode=self.TRADE_RETCODE_DONE,
                order=order_id,
                price=0.0,
                volume=0.0,
                comment="cancelled",
            )
        ticket = self._next_ticket
        self._next_ticket += 1
        if action == self.TRADE_ACTION_PENDING:
            self._pending.append(
                SimpleNamespace(
                    ticket=ticket,
                    magic=request.get("magic", 0),
                    symbol=request["symbol"],
                    type=request["type"],
                    volume_initial=request["volume"],
                    price_open=request["price"],
                    sl=request["sl"],
                    tp=request["tp"],
                    time_setup=int(datetime.now(timezone.utc).timestamp()),
                )
            )
            return SimpleNamespace(
                retcode=self.TRADE_RETCODE_DONE,
                order=ticket,
                price=request["price"],
                volume=request["volume"],
                comment="ok",
            )
        # Simulate an opened position when a market entry is sent.
        if "position" not in request:
            self._positions.append(
                SimpleNamespace(
                    ticket=ticket,
                    magic=request.get("magic", 0),
                    symbol=request["symbol"],
                    type=request["type"],
                    volume=request["volume"],
                    price_open=request["price"],
                    sl=request["sl"],
                    tp=request["tp"],
                    time=int(datetime.now(timezone.utc).timestamp()),
                    profit=0.0,
                )
            )
        else:
            # Close: drop the matched position.
            self._positions = [p for p in self._positions if int(p.ticket) != int(request["position"])]
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=ticket,
            price=request["price"],
            volume=request["volume"],
            comment="ok",
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class MT5BrokerTests(unittest.TestCase):
    def _broker(self, **fake_kwargs: Any) -> tuple[MT5LocalBroker, FakeMT5]:
        fake = FakeMT5(**fake_kwargs)
        broker = MT5LocalBroker(symbol=fake._symbol, _mt5=fake)
        broker.connect()
        return broker, fake

    def test_connect_resolves_symbol_spec(self) -> None:
        broker, fake = self._broker()
        self.assertTrue(fake._initialised)
        info = broker.get_account_info()
        self.assertEqual(info.equity, 10000.0)
        self.assertEqual(info.currency, "USD")

    def test_connect_fails_for_unknown_symbol(self) -> None:
        fake = FakeMT5(symbol="GOLD")
        broker = MT5LocalBroker(symbol="XAUUSD", _mt5=fake)
        with self.assertRaises(BrokerError):
            broker.connect()

    def test_get_open_position_returns_none_when_flat(self) -> None:
        broker, _ = self._broker()
        self.assertIsNone(broker.get_open_position())

    def test_place_buy_order_sizes_lots_correctly(self) -> None:
        # bid=4700, ask=4700.50 → BUY entry=4700.50
        # stop=4690.50 → distance=10 USD/oz, contract=100 oz/lot → $1000/lot
        # risk=$100 → 0.10 lots
        broker, fake = self._broker(bid=4700.0, ask=4700.50)
        req = OrderRequest(
            symbol="GOLD",
            side=OrderSide.BUY,
            risk_dollars=100.0,
            stop_price=4690.50,
            target_price=4720.50,
        )
        result = broker.place_market_order(req)
        self.assertTrue(result.accepted, result.error)
        self.assertAlmostEqual(result.units or 0.0, 0.10, places=2)
        self.assertAlmostEqual(result.fill_price or 0.0, 4700.50)
        sent = fake.sent_orders[-1]
        self.assertEqual(sent["type"], FakeMT5.ORDER_TYPE_BUY)
        self.assertEqual(sent["sl"], 4690.50)
        self.assertEqual(sent["tp"], 4720.50)
        self.assertEqual(sent["magic"], 20260507)

    def test_place_sell_order_uses_bid_and_correct_type(self) -> None:
        broker, fake = self._broker(bid=4700.0, ask=4700.50)
        req = OrderRequest(
            symbol="GOLD",
            side=OrderSide.SELL,
            risk_dollars=100.0,
            stop_price=4710.0,
            target_price=4680.0,
        )
        result = broker.place_market_order(req)
        self.assertTrue(result.accepted, result.error)
        self.assertEqual(fake.sent_orders[-1]["type"], FakeMT5.ORDER_TYPE_SELL)
        self.assertAlmostEqual(result.fill_price or 0.0, 4700.0)

    def test_rejects_invalid_buy_geometry(self) -> None:
        broker, _ = self._broker()
        # Stop above entry on BUY.
        req = OrderRequest("GOLD", OrderSide.BUY, 100.0, 5000.0, 4800.0)
        result = broker.place_market_order(req)
        self.assertFalse(result.accepted)
        self.assertIn("stop above entry", result.error or "")

    def test_rejects_invalid_sell_geometry(self) -> None:
        broker, _ = self._broker()
        # Target above entry on SELL.
        req = OrderRequest("GOLD", OrderSide.SELL, 100.0, 4710.0, 5000.0)
        result = broker.place_market_order(req)
        self.assertFalse(result.accepted)
        self.assertIn("target above entry", result.error or "")

    def test_stops_level_enforced(self) -> None:
        # trade_stops_level=100 points × point=0.01 = $1 minimum distance.
        broker, _ = self._broker(trade_stops_level=100, point=0.01)
        # 0.30 distance is below 1.0 minimum.
        req = OrderRequest("GOLD", OrderSide.BUY, 100.0, 4700.20, 4720.50)
        result = broker.place_market_order(req)
        self.assertFalse(result.accepted)
        self.assertIn("too close", result.error or "")

    def test_rejects_when_lots_below_min(self) -> None:
        # Very small risk → lots below volume_min.
        broker, _ = self._broker()
        req = OrderRequest("GOLD", OrderSide.BUY, 0.10, 4690.0, 4720.0)
        result = broker.place_market_order(req)
        self.assertFalse(result.accepted)
        self.assertIn("volume_min", result.error or "")

    def test_double_open_blocked_by_magic_filter(self) -> None:
        broker, _ = self._broker()
        req = OrderRequest("GOLD", OrderSide.BUY, 100.0, 4690.0, 4720.0)
        first = broker.place_market_order(req)
        self.assertTrue(first.accepted, first.error)
        second = broker.place_market_order(req)
        self.assertFalse(second.accepted)
        self.assertIn("already open", second.error or "")

    def test_get_open_position_filters_by_magic(self) -> None:
        # Pre-populate with two positions: one matching magic, one not.
        positions = [
            SimpleNamespace(
                ticket=11, magic=99999, symbol="GOLD", type=FakeMT5.POSITION_TYPE_BUY,
                volume=0.10, price_open=4700.0, sl=4690.0, tp=4720.0,
                time=int(datetime.now(timezone.utc).timestamp()), profit=0.0,
            ),
            SimpleNamespace(
                ticket=22, magic=20260507, symbol="GOLD", type=FakeMT5.POSITION_TYPE_BUY,
                volume=0.20, price_open=4701.0, sl=4691.0, tp=4721.0,
                time=int(datetime.now(timezone.utc).timestamp()), profit=5.0,
            ),
        ]
        broker, _ = self._broker(positions=positions)
        op = broker.get_open_position()
        self.assertIsNotNone(op)
        assert op is not None
        self.assertEqual(op.broker_order_id, "22")
        self.assertAlmostEqual(op.units, 0.20)
        self.assertAlmostEqual(op.entry_price, 4701.0)

    def test_close_position_round_trip(self) -> None:
        broker, fake = self._broker()
        req = OrderRequest("GOLD", OrderSide.BUY, 100.0, 4690.0, 4720.0)
        result = broker.place_market_order(req)
        self.assertTrue(result.accepted)
        ticket = result.broker_order_id
        assert ticket is not None
        trade = broker.close_position(ticket, reason="manual")
        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade.exit_reason, "manual")
        self.assertEqual(trade.side, OrderSide.BUY)
        # And we should now be flat.
        self.assertIsNone(broker.get_open_position())

    def test_close_position_unknown_returns_none(self) -> None:
        broker, _ = self._broker()
        self.assertIsNone(broker.close_position("doesnotexist"))

    # ------------------------------------------------------------------
    # Pending stop orders
    # ------------------------------------------------------------------
    def test_buy_stop_pending_when_entry_above_market(self) -> None:
        # bid=4700, ask=4700.50; entry 4720 is well above ask -> pending.
        broker, fake = self._broker()
        broker.connect()
        req = OrderRequest(
            symbol="GOLD",
            side=OrderSide.BUY,
            risk_dollars=100.0,
            stop_price=4690.0,
            target_price=4760.0,
            entry_price=4720.0,
        )
        result = broker.place_market_order(req)
        self.assertTrue(result.accepted, msg=result.error)
        sent = fake.sent_orders[-1]
        self.assertEqual(sent["action"], FakeMT5.TRADE_ACTION_PENDING)
        self.assertEqual(sent["type"], FakeMT5.ORDER_TYPE_BUY_STOP)
        self.assertAlmostEqual(sent["price"], 4720.0)
        # Lots sized off the chosen entry, not market price.
        # stop_distance = 4720 - 4690 = 30; loss/lot = 30*100 = 3000
        # lots = 100/3000 = 0.0333 -> floors to 0.03.
        self.assertAlmostEqual(sent["volume"], 0.03)

        pending = broker.get_pending_order()
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.side, OrderSide.BUY)
        self.assertAlmostEqual(pending.entry_price, 4720.0)

    def test_sell_stop_pending_when_entry_below_market(self) -> None:
        broker, fake = self._broker()
        broker.connect()
        req = OrderRequest(
            symbol="GOLD",
            side=OrderSide.SELL,
            risk_dollars=100.0,
            stop_price=4730.0,
            target_price=4660.0,
            entry_price=4680.0,  # below bid 4700 -> pending
        )
        result = broker.place_market_order(req)
        self.assertTrue(result.accepted, msg=result.error)
        sent = fake.sent_orders[-1]
        self.assertEqual(sent["action"], FakeMT5.TRADE_ACTION_PENDING)
        self.assertEqual(sent["type"], FakeMT5.ORDER_TYPE_SELL_STOP)

    def test_falls_back_to_market_when_entry_within_market(self) -> None:
        broker, fake = self._broker()
        broker.connect()
        req = OrderRequest(
            symbol="GOLD",
            side=OrderSide.BUY,
            risk_dollars=100.0,
            stop_price=4690.0,
            target_price=4760.0,
            entry_price=4700.40,  # 0.40 below current ask 4700.50 -> market
        )
        result = broker.place_market_order(req)
        self.assertTrue(result.accepted, msg=result.error)
        sent = fake.sent_orders[-1]
        self.assertEqual(sent["action"], FakeMT5.TRADE_ACTION_DEAL)
        self.assertEqual(sent["type"], FakeMT5.ORDER_TYPE_BUY)

    def test_cancel_pending_order(self) -> None:
        broker, _ = self._broker()
        broker.connect()
        req = OrderRequest(
            symbol="GOLD",
            side=OrderSide.BUY,
            risk_dollars=100.0,
            stop_price=4690.0,
            target_price=4760.0,
            entry_price=4720.0,
        )
        result = broker.place_market_order(req)
        self.assertTrue(result.accepted, msg=result.error)
        pending = broker.get_pending_order()
        self.assertIsNotNone(pending)
        assert pending is not None
        ok = broker.cancel_pending_order(pending.broker_order_id)
        self.assertTrue(ok)
        self.assertIsNone(broker.get_pending_order())

    def test_double_pending_blocked(self) -> None:
        broker, _ = self._broker()
        broker.connect()
        req = OrderRequest(
            symbol="GOLD",
            side=OrderSide.BUY,
            risk_dollars=100.0,
            stop_price=4690.0,
            target_price=4760.0,
            entry_price=4720.0,
        )
        self.assertTrue(broker.place_market_order(req).accepted)
        result = broker.place_market_order(req)
        self.assertFalse(result.accepted)
        self.assertIn("pending", (result.error or "").lower())


if __name__ == "__main__":
    unittest.main()
