"""Production infrastructure: structured logging, state DB, event bus, ledger.

This package is the production-grade backbone of the live agent.

Modules:
- ``logging_config``: configure stdlib ``logging`` with JSON formatter +
  rotating file handlers. Replaces ad-hoc ``print()`` statements.
- ``db``: SQLite-backed state store (positions, fills, events, ticks, bars,
  equity snapshots, signals). WAL mode, schema migrations, multi-process safe.
- ``events``: in-process pub/sub event bus with a durable JSONL mirror and
  SQLite persistence. All lifecycle moments (signal, decision, order_placed,
  order_filled, position_closed, kill_switch, bridge_error) flow through here.
- ``ledger``: pulls MT5 deal history, dedupes by deal_id, persists to the
  ``fills`` table, and emits ``position_closed`` events with realised P&L.

Pure standard library (no pandas, no pydantic, no SQLAlchemy).
"""

from .logging_config import (
    configure_logging,
    get_logger,
    JsonFormatter,
)
from .db import (
    StateDB,
    open_state_db,
    DEFAULT_DB_PATH,
)
from .events import (
    Event,
    EventBus,
    EventKind,
)
from .risk import (
    DivergenceConfig,
    DivergenceVerdict,
    EquityGuardConfig,
    EquityGuardVerdict,
    FlattenReport,
    TickAgeVerdict,
    evaluate_divergence_guard,
    evaluate_equity_guard,
    evaluate_tick_age,
    flatten_account,
    trip_kill_switch,
)
from .resource import apply_niceness, cpu_budget, resolve_workers


def __getattr__(name: str):
    if name == "sync_fills_ledger":
        from .ledger import sync_fills_ledger as _sync

        return _sync
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "configure_logging",
    "get_logger",
    "JsonFormatter",
    "StateDB",
    "open_state_db",
    "DEFAULT_DB_PATH",
    "Event",
    "EventBus",
    "EventKind",
    "sync_fills_ledger",
    "DivergenceConfig",
    "DivergenceVerdict",
    "EquityGuardConfig",
    "EquityGuardVerdict",
    "FlattenReport",
    "TickAgeVerdict",
    "evaluate_divergence_guard",
    "evaluate_equity_guard",
    "evaluate_tick_age",
    "flatten_account",
    "trip_kill_switch",
    "apply_niceness",
    "cpu_budget",
    "resolve_workers",
]
