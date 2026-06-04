"""MT5 broker — concrete implementation against the MetaTrader5 Python package.

Linux note
----------
The ``MetaTrader5`` package is Windows-only.  To use this on Linux you must
either:

1. Run a *Windows Python* under Wine that imports ``MetaTrader5``, exposed
   over HTTP via the bridge in ``mt5_bridge_server.py`` (Phase 5b).  Your
   Linux agent-cycle then uses ``MT5RemoteBroker`` (also Phase 5b).
2. Run the entire agent-cycle on a Windows host (laptop or VPS).

This module's ``MT5LocalBroker`` is what runs *on the Windows side* in either
deployment.  The lazy import means ``import gold_trader.live`` still works on
Linux — the import only fails when you actually instantiate the broker.

Lot sizing
----------
For XAUUSD / GOLD: 1 lot = 100 oz.  ``units = lots * contract_size``.
``risk_dollars`` is converted to lots via:

    stop_distance_quote = abs(entry - stop)               # USD per oz
    loss_per_lot = stop_distance_quote * contract_size    # USD per lot
    lots = risk_dollars / loss_per_lot

We then snap to ``volume_step`` and clamp to ``[min_lot, max_lot]``.

Symbol resolution
-----------------
Brokers vary: 'XAUUSD', 'XAUUSDm', 'XAUUSD.r', 'GOLD', 'GOLD.cmd', etc.
We accept the symbol as given and call ``symbol_select`` to ensure it's in
Market Watch.  No fuzzy matching — be explicit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .broker import (
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


# ---------------------------------------------------------------------------
# Lazy import shim — keeps import-time on Linux clean.
# ---------------------------------------------------------------------------
def _import_mt5() -> Any:
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise BrokerError(
            "MetaTrader5 package not installed.  This module only works "
            "under Windows / Wine with the MT5 terminal running.  "
            "Install with: pip install MetaTrader5"
        ) from exc
    return mt5


@dataclass(frozen=True)
class SymbolSpec:
    """Cached symbol metadata pulled from ``mt5.symbol_info``."""
    name: str
    contract_size: float        # oz per lot for XAUUSD/GOLD = 100
    volume_min: float           # smallest lot (e.g., 0.01)
    volume_max: float           # largest lot (e.g., 100)
    volume_step: float          # lot increment (e.g., 0.01)
    point: float                # smallest price increment (e.g., 0.01)
    digits: int                 # price decimals
    trade_stops_level: int      # min distance (in points) for SL/TP from price
    trade_freeze_level: int     # freeze distance (points)


def _round_to_step(value: float, step: float, *, mode: str = "down") -> float:
    """Round ``value`` to the nearest multiple of ``step``.

    mode='down' floors (used for lot sizing — never over-risk).
    """
    if step <= 0:
        return value
    if mode == "down":
        return (int(value / step)) * step
    return round(value / step) * step


class MT5LocalBroker:
    """Concrete broker driving MT5 via the MetaTrader5 Python package.

    Construction does *not* connect.  Call ``connect()`` first.  This keeps
    object construction side-effect-free for tests.
    """

    name: str = "mt5_local"

    def __init__(
        self,
        *,
        symbol: str = "GOLD",
        magic: int = 20260507,
        deviation_points: int = 20,
        account_type: str = "demo",
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        terminal_path: str | None = None,
        # Injection seam for tests — pass a fake mt5 module.
        _mt5: Any | None = None,
    ) -> None:
        self._symbol = symbol
        self._magic = magic
        self._deviation = deviation_points
        self._account_type = account_type.lower()
        self._login = login
        self._password = password
        self._server = server
        self._terminal_path = terminal_path
        self._mt5: Any | None = _mt5
        self._symbol_spec: SymbolSpec | None = None
        self._connected = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    def connect(self) -> None:
        """Initialise the MT5 terminal connection and resolve the symbol."""
        if self._connected:
            return
        mt5 = self._mt5 if self._mt5 is not None else _import_mt5()
        self._mt5 = mt5

        kwargs: dict[str, Any] = {}
        if self._terminal_path:
            kwargs["path"] = self._terminal_path
        if self._login is not None:
            kwargs["login"] = self._login
        if self._password is not None:
            kwargs["password"] = self._password
        if self._server is not None:
            kwargs["server"] = self._server

        ok = mt5.initialize(**kwargs) if kwargs else mt5.initialize()
        if not ok:
            err = self._last_error()
            raise BrokerError(f"mt5.initialize failed: {err}")

        # Resolve and cache the symbol spec.
        if not mt5.symbol_select(self._symbol, True):
            err = self._last_error()
            raise BrokerError(
                f"mt5.symbol_select('{self._symbol}', True) failed: {err}.  "
                f"Check the exact symbol shown in your Market Watch."
            )
        info = mt5.symbol_info(self._symbol)
        if info is None:
            raise BrokerError(f"mt5.symbol_info('{self._symbol}') returned None")
        self._symbol_spec = SymbolSpec(
            name=self._symbol,
            contract_size=float(info.trade_contract_size),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            point=float(info.point),
            digits=int(info.digits),
            trade_stops_level=int(getattr(info, "trade_stops_level", 0)),
            trade_freeze_level=int(getattr(info, "trade_freeze_level", 0)),
        )
        self._connected = True

    def shutdown(self) -> None:
        if self._mt5 is not None and self._connected:
            try:
                self._mt5.shutdown()
            except Exception:  # pragma: no cover
                pass
        self._connected = False

    def _ensure_connected(self) -> None:
        if not self._connected:
            self.connect()

    def _last_error(self) -> str:
        if self._mt5 is None:
            return "mt5 module not loaded"
        try:
            return str(self._mt5.last_error())
        except Exception:  # pragma: no cover
            return "unknown"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_account_info(self) -> AccountInfo:
        self._ensure_connected()
        mt5 = self._mt5
        info = mt5.account_info()
        if info is None:
            raise BrokerError(f"mt5.account_info returned None: {self._last_error()}")
        return AccountInfo(
            equity=float(info.equity),
            balance=float(info.balance),
            currency=str(info.currency),
            margin_used=float(info.margin),
            margin_free=float(info.margin_free),
            leverage=float(info.leverage),
        )

    def get_open_position(self, magic: int | None = None) -> OpenPosition | None:
        self._ensure_connected()
        mt5 = self._mt5
        magic_filter = self._magic if magic is None else magic
        positions = mt5.positions_get(symbol=self._symbol)
        if positions is None:
            # MT5 returns None on error rather than empty tuple.  Distinguish
            # via last_error.
            err_code = getattr(mt5, "RES_S_OK", 1)
            last = mt5.last_error()
            if isinstance(last, tuple) and last and last[0] != err_code:
                raise BrokerError(f"mt5.positions_get error: {last}")
            return None
        for p in positions:
            if int(p.magic) != magic_filter:
                continue
            side = OrderSide.BUY if int(p.type) == mt5.POSITION_TYPE_BUY else OrderSide.SELL
            opened_at = datetime.fromtimestamp(int(p.time), tz=timezone.utc)
            return OpenPosition(
                broker_order_id=str(p.ticket),
                symbol=str(p.symbol),
                side=side,
                units=float(p.volume),
                entry_price=float(p.price_open),
                stop_price=float(p.sl),
                target_price=float(p.tp),
                opened_at=opened_at,
                unrealised_pnl=float(p.profit),
                magic=int(p.magic),
            )
        return None

    def place_market_order(self, request: OrderRequest) -> OrderResult:
        """Place a market deal OR a pending stop, based on ``request.entry_price``.

        * ``entry_price`` is None → market deal at current bid/ask.
        * ``entry_price`` is set and beyond stops-level from market → pending
          BUY_STOP / SELL_STOP at that level.
        * ``entry_price`` is set but within stops-level → fall back to market
          (best-effort: signal already triggered).
        """
        self._ensure_connected()
        mt5 = self._mt5
        spec = self._symbol_spec
        if spec is None:
            raise BrokerError("symbol spec not loaded — call connect() first")

        # Defensive double-check: refuse to place a second position OR pending.
        if self.get_open_position(self._magic) is not None:
            return OrderResult(
                accepted=False, broker_order_id=None, fill_price=None,
                units=None, error="position already open under this magic",
            )
        if self.get_pending_order(self._magic) is not None:
            return OrderResult(
                accepted=False, broker_order_id=None, fill_price=None,
                units=None, error="pending order already resting under this magic",
            )

        # Get current tick.
        tick = mt5.symbol_info_tick(spec.name)
        if tick is None:
            return OrderResult(
                accepted=False, broker_order_id=None, fill_price=None,
                units=None, error=f"no tick for {spec.name}",
            )
        market_ref = float(tick.ask) if request.side is OrderSide.BUY else float(tick.bid)

        # Decide market vs pending.
        min_distance = spec.trade_stops_level * spec.point
        use_pending = False
        entry: float
        if request.entry_price is not None:
            requested = float(request.entry_price)
            if request.side is OrderSide.BUY:
                # BUY_STOP must be ABOVE current ask by at least min_distance.
                if requested > market_ref + max(min_distance, spec.point):
                    use_pending = True
                    entry = requested
                else:
                    entry = market_ref  # fall back to market
            else:
                # SELL_STOP must be BELOW current bid by at least min_distance.
                if requested < market_ref - max(min_distance, spec.point):
                    use_pending = True
                    entry = requested
                else:
                    entry = market_ref
        else:
            entry = market_ref

        # Validate stop/target geometry against the chosen entry.
        if request.side is OrderSide.BUY:
            if request.stop_price >= entry:
                return OrderResult(False, None, None, None, "stop above entry on BUY")
            if request.target_price <= entry:
                return OrderResult(False, None, None, None, "target below entry on BUY")
        else:
            if request.stop_price <= entry:
                return OrderResult(False, None, None, None, "stop below entry on SELL")
            if request.target_price >= entry:
                return OrderResult(False, None, None, None, "target above entry on SELL")

        # Stops-level check: SL/TP must be at least min_distance from entry.
        if min_distance > 0:
            if abs(entry - request.stop_price) < min_distance:
                return OrderResult(
                    False, None, None, None,
                    f"stop too close: {abs(entry - request.stop_price):.4f} < min {min_distance:.4f}",
                )
            if abs(entry - request.target_price) < min_distance:
                return OrderResult(
                    False, None, None, None,
                    f"target too close: {abs(entry - request.target_price):.4f} < min {min_distance:.4f}",
                )

        # Lot sizing — based on the chosen entry.
        stop_distance = abs(entry - request.stop_price)
        if stop_distance <= 0:
            return OrderResult(False, None, None, None, "zero stop distance")
        loss_per_lot = stop_distance * spec.contract_size
        if loss_per_lot <= 0:
            return OrderResult(False, None, None, None, "zero loss-per-lot")
        raw_lots = request.risk_dollars / loss_per_lot
        lots = _round_to_step(raw_lots, spec.volume_step, mode="down")
        if lots < spec.volume_min:
            return OrderResult(
                False, None, None, None,
                f"lots {lots:.4f} below volume_min {spec.volume_min}",
            )
        if lots > spec.volume_max:
            lots = spec.volume_max

        # Build order dict for either path.
        if use_pending:
            if request.side is OrderSide.BUY:
                order_type = mt5.ORDER_TYPE_BUY_STOP
            else:
                order_type = mt5.ORDER_TYPE_SELL_STOP
            order = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": spec.name,
                "volume": float(lots),
                "type": order_type,
                "price": float(round(entry, spec.digits)),
                "sl": float(round(request.stop_price, spec.digits)),
                "tp": float(round(request.target_price, spec.digits)),
                "deviation": int(self._deviation),
                "magic": int(self._magic),
                "comment": (request.comment or "gold-trader")[:30],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN,
            }
        else:
            order_type = (
                mt5.ORDER_TYPE_BUY if request.side is OrderSide.BUY
                else mt5.ORDER_TYPE_SELL
            )
            order = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": spec.name,
                "volume": float(lots),
                "type": order_type,
                "price": entry,
                "sl": float(round(request.stop_price, spec.digits)),
                "tp": float(round(request.target_price, spec.digits)),
                "deviation": int(self._deviation),
                "magic": int(self._magic),
                "comment": (request.comment or "gold-trader")[:30],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
        result = mt5.order_send(order)
        if result is None:
            return OrderResult(False, None, None, None, f"order_send returned None: {self._last_error()}")
        retcode = int(result.retcode)
        if retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(
                False, None, None, None,
                f"order_send retcode={retcode} comment={getattr(result, 'comment', '')}",
            )
        return OrderResult(
            accepted=True,
            broker_order_id=str(result.order),
            fill_price=float(result.price) if not use_pending else float(entry),
            units=float(result.volume) if float(getattr(result, "volume", 0)) > 0 else float(lots),
            error=None,
        )

    def close_position(
        self,
        broker_order_id: str,
        reason: str = "manual",
    ) -> ClosedTrade | None:
        self._ensure_connected()
        mt5 = self._mt5
        spec = self._symbol_spec
        if spec is None:
            raise BrokerError("symbol spec not loaded — call connect() first")

        positions = mt5.positions_get(symbol=spec.name)
        if not positions:
            return None
        target = next((p for p in positions if str(p.ticket) == broker_order_id), None)
        if target is None:
            return None

        tick = mt5.symbol_info_tick(spec.name)
        if tick is None:
            raise BrokerError(f"no tick for {spec.name}")
        if int(target.type) == mt5.POSITION_TYPE_BUY:
            close_price = float(tick.bid)
            close_type = mt5.ORDER_TYPE_SELL
            opened_side = OrderSide.BUY
        else:
            close_price = float(tick.ask)
            close_type = mt5.ORDER_TYPE_BUY
            opened_side = OrderSide.SELL

        order = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": float(target.volume),
            "type": close_type,
            "position": int(target.ticket),
            "price": close_price,
            "deviation": int(self._deviation),
            "magic": int(self._magic),
            "comment": f"close:{reason}"[:30],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(order)
        if result is None or int(result.retcode) != mt5.TRADE_RETCODE_DONE:
            err = getattr(result, "comment", self._last_error())
            raise BrokerError(f"close failed: retcode={getattr(result, 'retcode', None)} {err}")
        opened_at = datetime.fromtimestamp(int(target.time), tz=timezone.utc)
        return ClosedTrade(
            broker_order_id=broker_order_id,
            symbol=spec.name,
            side=opened_side,
            units=float(target.volume),
            entry_price=float(target.price_open),
            exit_price=float(result.price),
            opened_at=opened_at,
            closed_at=datetime.now(timezone.utc),
            pnl_dollars=float(target.profit),
            exit_reason=reason,
        )

    # ------------------------------------------------------------------
    # Pending orders
    # ------------------------------------------------------------------
    def get_pending_order(self, magic: int = 20260507) -> PendingOrder | None:
        self._ensure_connected()
        mt5 = self._mt5
        spec = self._symbol_spec
        if spec is None:
            raise BrokerError("symbol spec not loaded — call connect() first")
        orders = mt5.orders_get(symbol=spec.name)
        if not orders:
            return None
        stop_types = {
            int(getattr(mt5, "ORDER_TYPE_BUY_STOP", -1)),
            int(getattr(mt5, "ORDER_TYPE_SELL_STOP", -2)),
        }
        for o in orders:
            if int(o.magic) != int(magic):
                continue
            otype = int(o.type)
            if otype not in stop_types:
                continue
            side = (
                OrderSide.BUY if otype == int(mt5.ORDER_TYPE_BUY_STOP)
                else OrderSide.SELL
            )
            placed_at = datetime.fromtimestamp(int(o.time_setup), tz=timezone.utc)
            return PendingOrder(
                broker_order_id=str(o.ticket),
                symbol=str(o.symbol),
                side=side,
                units=float(o.volume_initial),
                entry_price=float(o.price_open),
                stop_price=float(o.sl),
                target_price=float(o.tp),
                placed_at=placed_at,
                magic=int(o.magic),
            )
        return None

    def cancel_pending_order(self, broker_order_id: str) -> bool:
        self._ensure_connected()
        mt5 = self._mt5
        order = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": int(broker_order_id),
        }
        result = mt5.order_send(order)
        if result is None:
            return False
        return int(result.retcode) == int(mt5.TRADE_RETCODE_DONE)

    # ------------------------------------------------------------------
    # Deal history (fills ledger)
    # ------------------------------------------------------------------
    def get_deals_since(
        self,
        since: datetime,
        magic: int = 20260507,
    ) -> list[Deal]:
        self._ensure_connected()
        mt5 = self._mt5
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        # mt5.history_deals_get accepts (date_from, date_to) as datetimes;
        # use a generous future ceiling so we always grab everything since.
        date_to = datetime.now(timezone.utc) + timedelta(days=1)
        try:
            deals = mt5.history_deals_get(since, date_to)
        except Exception as exc:  # pragma: no cover - mt5 surface is variable
            raise BrokerError(f"history_deals_get failed: {exc}") from exc
        if deals is None:
            return []
        out: list[Deal] = []
        for d in deals:
            if magic and int(getattr(d, "magic", 0)) != int(magic):
                continue
            try:
                deal_type = int(d.type)
                # MT5: DEAL_TYPE_BUY=0, DEAL_TYPE_SELL=1; entry_type 0=in,1=out
                side = OrderSide.BUY if deal_type == 0 else OrderSide.SELL
                out.append(
                    Deal(
                        deal_id=str(d.ticket),
                        order_ticket=str(getattr(d, "order", "")) or None,
                        position_ticket=str(getattr(d, "position_id", "")) or None,
                        symbol=str(d.symbol),
                        side=side,
                        volume=float(d.volume),
                        price=float(d.price),
                        profit=float(getattr(d, "profit", 0.0)),
                        swap=float(getattr(d, "swap", 0.0)),
                        commission=float(getattr(d, "commission", 0.0)),
                        fee=float(getattr(d, "fee", 0.0)),
                        deal_type=deal_type,
                        entry_type=int(getattr(d, "entry", 0)),
                        reason=int(getattr(d, "reason", 0)),
                        time=datetime.fromtimestamp(int(d.time), tz=timezone.utc),
                        magic=int(getattr(d, "magic", 0)),
                        comment=str(getattr(d, "comment", "") or ""),
                    )
                )
            except Exception:
                # skip malformed entries; better to keep going than abort.
                continue
        return out
