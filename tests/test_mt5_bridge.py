"""End-to-end tests for the MT5 bridge: real HTTP server + remote client."""
from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from typing import Any

from gold_trader.live.broker import (
    AccountInfo,
    BrokerError,
    ClosedTrade,
    Deal,
    OpenPosition,
    OrderRequest,
    OrderResult,
    OrderSide,
    PendingOrder,
)
from gold_trader.live.mt5_bridge_client import MT5RemoteBroker
from gold_trader.live.mt5_bridge_server import BridgeHandler


class FakeBroker:
    """Stand-in for MT5LocalBroker — covers Broker Protocol surface."""

    name = "fake_mt5"

    def __init__(self) -> None:
        self._open: OpenPosition | None = None
        self._pending: PendingOrder | None = None
        self._deals: list[Deal] = []

    def get_account_info(self) -> AccountInfo:
        return AccountInfo(10000.0, 10000.0, "USD", 0.0, 10000.0, 100.0)

    def get_open_position(self, magic: int = 20260507) -> OpenPosition | None:
        if self._open is None:
            return None
        if self._open.magic != magic:
            return None
        return self._open

    def get_pending_order(self, magic: int = 20260507) -> PendingOrder | None:
        if self._pending is None:
            return None
        if self._pending.magic != magic:
            return None
        return self._pending

    def cancel_pending_order(self, broker_order_id: str) -> bool:
        if self._pending is not None and self._pending.broker_order_id == broker_order_id:
            self._pending = None
            return True
        return False

    def get_deals_since(
        self, since: datetime, magic: int = 20260507,
    ) -> list[Deal]:
        return [d for d in self._deals if d.time >= since and d.magic == magic]

    def place_market_order(self, request: OrderRequest) -> OrderResult:
        if self._open is not None or self._pending is not None:
            return OrderResult(False, None, None, None, "already open")
        if request.entry_price is not None and (
            (request.side is OrderSide.BUY and request.entry_price > 4701.0)
            or (request.side is OrderSide.SELL and request.entry_price < 4699.0)
        ):
            self._pending = PendingOrder(
                broker_order_id="8001",
                symbol=request.symbol,
                side=request.side,
                units=0.1,
                entry_price=request.entry_price,
                stop_price=request.stop_price,
                target_price=request.target_price,
                placed_at=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
                magic=request.magic,
            )
            return OrderResult(True, "8001", request.entry_price, 0.1, None)
        self._open = OpenPosition(
            broker_order_id="9001",
            symbol=request.symbol,
            side=request.side,
            units=0.1,
            entry_price=4700.0,
            stop_price=request.stop_price,
            target_price=request.target_price,
            opened_at=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
            unrealised_pnl=0.0,
            magic=request.magic,
        )
        return OrderResult(True, "9001", 4700.0, 0.1, None)

    def close_position(self, broker_order_id: str, reason: str = "manual") -> ClosedTrade | None:
        if self._open is None or self._open.broker_order_id != broker_order_id:
            return None
        op = self._open
        self._open = None
        return ClosedTrade(
            broker_order_id=op.broker_order_id,
            symbol=op.symbol,
            side=op.side,
            units=op.units,
            entry_price=op.entry_price,
            exit_price=op.entry_price,
            opened_at=op.opened_at,
            closed_at=datetime(2026, 5, 7, 13, 0, tzinfo=timezone.utc),
            pnl_dollars=0.0,
            exit_reason=reason,
        )


class _BridgeFixture:
    def __init__(self, secret: str = "") -> None:
        self.broker = FakeBroker()
        BridgeHandler.broker = self.broker
        BridgeHandler.secret = secret
        # Port 0 = OS picks a free one.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), BridgeHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class BridgeRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _BridgeFixture()
        self.client = MT5RemoteBroker(base_url=self.fx.url())

    def tearDown(self) -> None:
        self.fx.close()

    def test_healthz(self) -> None:
        h = self.client.healthz()
        self.assertTrue(h.get("ok"))
        self.assertEqual(h.get("broker"), "fake_mt5")

    def test_account_info(self) -> None:
        info = self.client.get_account_info()
        self.assertEqual(info.equity, 10000.0)
        self.assertEqual(info.currency, "USD")

    def test_position_when_flat(self) -> None:
        self.assertIsNone(self.client.get_open_position())

    def test_full_order_lifecycle(self) -> None:
        req = OrderRequest("GOLD", OrderSide.BUY, 100.0, 4690.0, 4720.0)
        r = self.client.place_market_order(req)
        self.assertTrue(r.accepted)
        self.assertEqual(r.broker_order_id, "9001")

        op = self.client.get_open_position()
        self.assertIsNotNone(op)
        assert op is not None
        self.assertEqual(op.broker_order_id, "9001")
        self.assertEqual(op.side, OrderSide.BUY)

        trade = self.client.close_position("9001", reason="manual")
        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade.exit_reason, "manual")

        self.assertIsNone(self.client.get_open_position())

    def test_pending_order_roundtrip(self) -> None:
        # Initial state — flat.
        self.assertIsNone(self.client.get_pending_order())

        # entry_price far above market triggers pending-stop branch.
        req = OrderRequest(
            symbol="GOLD",
            side=OrderSide.BUY,
            risk_dollars=50.0,
            stop_price=4690.0,
            target_price=4760.0,
            entry_price=4720.0,
        )
        res = self.client.place_market_order(req)
        self.assertTrue(res.accepted, msg=res.error)
        self.assertEqual(res.broker_order_id, "8001")

        pending = self.client.get_pending_order()
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.broker_order_id, "8001")
        self.assertEqual(pending.entry_price, 4720.0)
        self.assertEqual(pending.side, OrderSide.BUY)

        ok = self.client.cancel_pending_order("8001")
        self.assertTrue(ok)
        self.assertIsNone(self.client.get_pending_order())

    def test_deals_since_roundtrip(self) -> None:
        # Seed a couple of fake deals on the broker.
        t0 = datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc)
        d1 = Deal(
            deal_id="111", order_ticket="O1", position_ticket="P1",
            symbol="GOLD", side=OrderSide.BUY, volume=0.01, price=4700.0,
            profit=0.0, swap=0.0, commission=0.0, fee=0.0,
            deal_type=0, entry_type=0, reason=0, time=t0,
            magic=20260507, comment="entry",
        )
        d2 = Deal(
            deal_id="222", order_ticket="O1", position_ticket="P1",
            symbol="GOLD", side=OrderSide.SELL, volume=0.01, price=4720.0,
            profit=20.0, swap=0.0, commission=0.0, fee=0.0,
            deal_type=1, entry_type=1, reason=0,
            time=t0 + timedelta(minutes=30), magic=20260507, comment="tp",
        )
        self.fx.broker._deals = [d1, d2]
        deals = self.client.get_deals_since(t0 - timedelta(minutes=5))
        self.assertEqual(len(deals), 2)
        self.assertEqual(deals[0].deal_id, "111")
        self.assertEqual(deals[1].entry_type, 1)
        # Magic filter respected — none after bumping window.
        empty = self.client.get_deals_since(
            t0 + timedelta(hours=1)
        )
        self.assertEqual(empty, [])


class BridgeAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _BridgeFixture(secret="hunter2")

    def tearDown(self) -> None:
        self.fx.close()

    def test_missing_secret_rejected(self) -> None:
        client = MT5RemoteBroker(base_url=self.fx.url())  # no secret
        with self.assertRaises(BrokerError) as ctx:
            client.get_account_info()
        self.assertIn("401", str(ctx.exception))

    def test_correct_secret_accepted(self) -> None:
        client = MT5RemoteBroker(base_url=self.fx.url(), shared_secret="hunter2")
        info = client.get_account_info()
        self.assertEqual(info.equity, 10000.0)


class BridgeUnreachableTests(unittest.TestCase):
    def test_connection_refused(self) -> None:
        # Port 1 is unlikely to have anything listening.
        client = MT5RemoteBroker(base_url="http://127.0.0.1:1", timeout=2.0)
        with self.assertRaises(BrokerError) as ctx:
            client.get_account_info()
        self.assertIn("unreachable", str(ctx.exception).lower())


class _FakeTick:
    def __init__(self, ts: float, bid: float, ask: float) -> None:
        self.time = ts
        self.bid = bid
        self.ask = ask
        self.last = bid
        self.volume = 1
        self.flags = 0


class TickFeedTests(unittest.TestCase):
    def test_tick_feed_polls_and_buffers(self) -> None:
        from gold_trader.live.mt5_bridge_server import TickFeed

        ticks = [
            _FakeTick(1714000000.0, 4700.0, 4700.5),
            _FakeTick(1714000001.0, 4700.1, 4700.6),
            _FakeTick(1714000002.0, 4700.2, 4700.7),
        ]
        idx = {"i": 0}
        def poll(symbol: str):
            i = idx["i"]
            idx["i"] = min(i + 1, len(ticks) - 1)
            return ticks[i]

        feed = TickFeed(symbol="GOLD", poll_fn=poll, interval_sec=0.01, maxlen=50)
        feed.start()
        time.sleep(0.1)
        feed.stop()

        snap = feed.snapshot()
        self.assertGreaterEqual(len(snap), 3)  # all distinct ticks recorded
        self.assertEqual(snap[0]["bid"], 4700.0)
        self.assertEqual(feed.last_tick()["bid"], 4700.2)
        h = feed.health()
        self.assertEqual(h["symbol"], "GOLD")
        self.assertGreaterEqual(h["buffer_size"], 3)
        self.assertIsNotNone(h["last_tick_ts"])

    def test_tick_feed_dedupes_same_timestamp(self) -> None:
        from gold_trader.live.mt5_bridge_server import TickFeed

        # Always returns same tick.
        same = _FakeTick(1714000000.0, 4700.0, 4700.5)
        feed = TickFeed(
            symbol="GOLD", poll_fn=lambda s: same,
            interval_sec=0.01, maxlen=50,
        )
        feed.start()
        time.sleep(0.06)
        feed.stop()
        # First poll records the tick; subsequent identical ones dedupe.
        self.assertEqual(len(feed.snapshot()), 1)


class BridgeTicksRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        from gold_trader.live.mt5_bridge_server import TickFeed

        self.fx = _BridgeFixture()
        # Inject a synthetic tick feed into the handler.
        ticks = [
            _FakeTick(1714000000.0, 4700.0, 4700.5),
            _FakeTick(1714000001.0, 4700.1, 4700.6),
        ]
        idx = {"i": 0}
        def poll(symbol: str):
            i = idx["i"]
            idx["i"] = min(i + 1, len(ticks) - 1)
            return ticks[i]
        self.feed = TickFeed(
            symbol="GOLD", poll_fn=poll, interval_sec=0.01, maxlen=50,
        )
        self.feed.start()
        time.sleep(0.1)
        BridgeHandler.tick_feed = self.feed
        self.client = MT5RemoteBroker(base_url=self.fx.url())

    def tearDown(self) -> None:
        BridgeHandler.tick_feed = None
        self.feed.stop()
        self.fx.close()

    def test_last_tick_endpoint(self) -> None:
        last = self.client.get_last_tick()
        self.assertIsNotNone(last)
        self.assertEqual(last["symbol"], "GOLD")
        self.assertEqual(last["bid"], 4700.1)

    def test_ticks_since_endpoint(self) -> None:
        all_ticks = self.client.get_ticks_since(
            datetime(1970, 1, 1, tzinfo=timezone.utc)
        )
        self.assertGreaterEqual(len(all_ticks), 2)
        # Future cutoff should return nothing.
        future = self.client.get_ticks_since(
            datetime(2099, 1, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(future, [])

    def test_healthz_includes_tick_feed(self) -> None:
        h = self.client.healthz()
        self.assertIn("tick_feed", h)
        self.assertEqual(h["tick_feed"]["symbol"], "GOLD")
        self.assertGreaterEqual(h["tick_feed"]["buffer_size"], 1)


if __name__ == "__main__":
    unittest.main()
