"""``gold_trader.live`` — broker abstraction layer.

The agent-cycle and any external orchestration should import the ``Broker``
Protocol and the ``get_broker_from_env`` helper from this package.

Concrete brokers:
* ``PaperBroker``  — wraps the in-process PaperState (fully implemented today).
* ``MT5LocalBroker``  — pending Phase 5b; runs on Windows / Wine.
* ``MT5RemoteBroker`` — pending Phase 5b; HTTP client to a VPS-side bridge.
"""
from .broker import (
    AccountInfo,
    Broker,
    BrokerError,
    ClosedTrade,
    Deal,
    OpenPosition,
    OrderRequest,
    OrderResult,
    OrderSide,
    PendingOrder,
)
from .paper_broker import PaperBroker, get_broker_from_env

# MT5 broker is import-safe (lazy MetaTrader5 import); construction may fail
# on Linux without Wine, but the symbol is always available.
from .mt5_broker import MT5LocalBroker, SymbolSpec
from .mt5_bridge_client import MT5RemoteBroker
from .agent_state import (
    LiveAgentState,
    load_live_state,
    save_live_state,
    serialize_closed_trade,
)

__all__ = [
    "AccountInfo",
    "Broker",
    "BrokerError",
    "ClosedTrade",
    "Deal",
    "LiveAgentState",
    "MT5LocalBroker",
    "MT5RemoteBroker",
    "OpenPosition",
    "OrderRequest",
    "OrderResult",
    "OrderSide",
    "PaperBroker",
    "PendingOrder",
    "SymbolSpec",
    "get_broker_from_env",
    "load_live_state",
    "save_live_state",
    "serialize_closed_trade",
]
