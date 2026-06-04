"""Broker abstraction.

A single small Protocol that hides whether trades are paper-only, executed via
local MT5 (Wine), or via a remote MT5 bridge running on a VPS.  The agent-cycle
calls this interface; concrete brokers plug in via env var.

Design tenets
-------------
* Risk-sized in **dollars and price**, not lots/pips.  Lot sizing is the
  broker's responsibility because contract size and min_lot vary per broker.
* Stops and targets are price levels, not distances.  Cleaner for math.
* No async.  Cron-driven cycles tolerate ~1s blocking RPC fine.
* No global state.  Each cycle constructs a broker, uses it, discards it.
* Errors are explicit: ``BrokerError`` for any non-recoverable issue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol


class BrokerError(RuntimeError):
    """Raised by broker implementations for any non-recoverable failure."""


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OrderRequest:
    """Inputs to ``place_market_order``.

    Attributes
    ----------
    symbol : str
        Broker-specific symbol (e.g., 'XAUUSD', 'XAUUSD.r').  Must match the
        instrument the broker supports.
    side : OrderSide
        Market direction.
    risk_dollars : float
        Dollar risk if stop is hit.  Broker computes lot size from this and
        the stop distance.  Must be > 0.
    stop_price : float
        Hard stop level.  Broker submits this as a server-side stop.
    target_price : float
        Take-profit level.  Broker submits as TP.
    magic : int
        Identifier the broker uses to mark orders from this system.
        Defaults to a fixed value so manual orders are not affected.
    comment : str
        Short free-text label, capped at ~30 chars.  Useful for tracing.
    """
    symbol: str
    side: OrderSide
    risk_dollars: float
    stop_price: float
    target_price: float
    entry_price: float | None = None
    """If set, place a *pending stop* order at this price (BUY_STOP for BUY,
    SELL_STOP for SELL).  If None, place an immediate market deal.

    Pending stop semantics: the broker will fill only when price reaches
    ``entry_price``.  This matches breakout-strategy intent (paper-mode
    fills only when price crosses the breakout level).  Implementations
    fall back to a market deal when ``entry_price`` is within one
    ``trade_stops_level`` worth of the current bid/ask, since most
    brokers reject stops too close to market.
    """
    magic: int = 20260507
    comment: str = ""


@dataclass(frozen=True)
class OrderResult:
    """Outcome of ``place_market_order``."""
    accepted: bool
    broker_order_id: str | None
    fill_price: float | None
    units: float | None
    error: str | None = None


@dataclass(frozen=True)
class OpenPosition:
    """Snapshot of the currently-open position, if any."""
    broker_order_id: str
    symbol: str
    side: OrderSide
    units: float
    entry_price: float
    stop_price: float
    target_price: float
    opened_at: datetime
    unrealised_pnl: float
    magic: int = 20260507


@dataclass(frozen=True)
class ClosedTrade:
    """Result of a position close — either by stop/target hit or manual."""
    broker_order_id: str
    symbol: str
    side: OrderSide
    units: float
    entry_price: float
    exit_price: float
    opened_at: datetime
    closed_at: datetime
    pnl_dollars: float
    exit_reason: str  # "stop", "target", "manual", "kill_switch"


@dataclass(frozen=True)
class AccountInfo:
    """Account state snapshot."""
    equity: float
    balance: float
    currency: str
    margin_used: float
    margin_free: float
    leverage: float


@dataclass(frozen=True)
class PendingOrder:
    """A stop/limit order placed at the broker but not yet filled."""
    broker_order_id: str
    symbol: str
    side: OrderSide
    units: float
    entry_price: float
    stop_price: float
    target_price: float
    placed_at: datetime
    magic: int = 20260507


@dataclass(frozen=True)
class Deal:
    """A single broker deal record (fill leg of an order).

    Mirrors the subset of the MT5 ``HistoryDeal`` we care about.  Two deals
    typically make a round-trip: ``entry_type=DEAL_ENTRY_IN`` opens a
    position, ``entry_type=DEAL_ENTRY_OUT`` closes it; matching them by
    ``position_ticket`` yields realised P&L.
    """

    deal_id: str
    order_ticket: str | None
    position_ticket: str | None
    symbol: str
    side: OrderSide
    volume: float
    price: float
    profit: float
    swap: float
    commission: float
    fee: float
    deal_type: int
    entry_type: int
    reason: int
    time: datetime
    magic: int = 0
    comment: str = ""


class Broker(Protocol):
    """The contract every concrete broker must satisfy."""

    name: str  # short identifier ("paper", "mt5_local", "mt5_remote")

    def get_account_info(self) -> AccountInfo:
        """Return account equity / margin state."""
        ...

    def get_open_position(self, magic: int = 20260507) -> OpenPosition | None:
        """Return the system's open position, or None if flat.

        The ``magic`` filter ensures we only return orders our system placed,
        ignoring any manual trades the user has running on the same account.
        """
        ...

    def place_market_order(self, request: OrderRequest) -> OrderResult:
        """Submit a market order with attached stop and target.

        Implementations must reject if a position is already open under the
        same magic number — the caller (agent-cycle) is responsible for not
        oversending.  Defensive double-check here is cheap insurance.
        """
        ...

    def close_position(
        self,
        broker_order_id: str,
        reason: str = "manual",
    ) -> ClosedTrade | None:
        """Close the position with the given broker order id at market.

        Returns the realised trade record, or None if the position is no longer
        open (e.g., stop/target already hit between cycles).
        """
        ...

    def get_pending_order(self, magic: int = 20260507) -> "PendingOrder | None":
        """Return the system's currently-resting pending order, or None.

        Pending orders that have not yet triggered live in the broker's
        order book (separate from positions).  agent-cycle queries this so
        it does not double-place a breakout entry.
        """
        ...

    def cancel_pending_order(self, broker_order_id: str) -> bool:
        """Cancel a resting pending order.  Returns True if cancelled."""
        ...

    def get_deals_since(
        self,
        since: datetime,
        magic: int = 20260507,
    ) -> list[Deal]:
        """Return broker deals (fills) at-or-after ``since``, magic-filtered.

        Used by the fills ledger to reconstruct closed-trade history without
        relying on between-cycle polling.  Implementations should be
        idempotent — callers will dedupe by ``deal_id``.
        """
        ...
