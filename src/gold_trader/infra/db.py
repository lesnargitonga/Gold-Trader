"""SQLite-backed state store for the live agent.

Replaces ad-hoc JSON files (``paper_state.json``, ``live_state.json``,
``events.jsonl``) with one durable, queryable, multi-process-safe database.

Design choices
--------------
* WAL mode for concurrent reads while the agent writes.
* Schema versioning via ``PRAGMA user_version``; migrations are pure-SQL
  forward-only.
* Plain-stdlib ``sqlite3`` only.
* Times are always stored as UTC ISO-8601 strings (``Z`` suffix).
* Idempotent ``open_state_db()`` — first call creates schema; later calls
  attach to it.

Tables
~~~~~~
positions
    open and historical positions (one row per ticket).
pending_orders
    resting pending stop orders.
fills
    individual broker deal records (long-term ledger; closed-trade truth).
events
    durable event log (mirror of events.jsonl) — see ``events.py``.
equity_snapshots
    one row per agent cycle: equity, balance, paper_equity, broker, ts.
signals
    every signal emitted by every strategy on every iteration.
ticks
    raw bid/ask/last from MT5 (Phase B; created here so schema is ready).
bars
    aggregated OHLCV (Phase B; created here so schema is ready).

This module is intentionally low-level: it owns the schema and exposes
small CRUD helpers. Higher-level objects (``EventBus``, ledger, paper
state) build on top.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB_PATH = Path("data/state.db")
SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS positions (
    ticket TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    units REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    closed_price REAL,
    pnl_dollars REAL,
    exit_reason TEXT,
    magic INTEGER NOT NULL,
    family TEXT,
    timeframe_minutes INTEGER,
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_opened ON positions(opened_at);

CREATE TABLE IF NOT EXISTS pending_orders (
    ticket TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    units REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL NOT NULL,
    placed_at TEXT NOT NULL,
    cancelled_at TEXT,
    magic INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'resting'
);

CREATE TABLE IF NOT EXISTS fills (
    deal_id TEXT PRIMARY KEY,
    order_ticket TEXT,
    position_ticket TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    volume REAL NOT NULL,
    price REAL NOT NULL,
    profit REAL NOT NULL DEFAULT 0,
    swap REAL NOT NULL DEFAULT 0,
    commission REAL NOT NULL DEFAULT 0,
    fee REAL NOT NULL DEFAULT 0,
    deal_type INTEGER NOT NULL,
    entry_type INTEGER NOT NULL,
    reason INTEGER NOT NULL DEFAULT 0,
    time TEXT NOT NULL,
    magic INTEGER NOT NULL DEFAULT 0,
    comment TEXT
);
CREATE INDEX IF NOT EXISTS idx_fills_position ON fills(position_ticket);
CREATE INDEX IF NOT EXISTS idx_fills_time ON fills(time);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    correlation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts);
CREATE INDEX IF NOT EXISTS idx_events_corr ON events(correlation_id);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    broker_name TEXT NOT NULL,
    equity REAL NOT NULL,
    balance REAL NOT NULL,
    margin_used REAL NOT NULL DEFAULT 0,
    margin_free REAL NOT NULL DEFAULT 0,
    paper_equity REAL,
    open_position_count INTEGER NOT NULL DEFAULT 0,
    pending_order_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    family TEXT NOT NULL,
    timeframe_minutes INTEGER NOT NULL,
    side TEXT NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    score REAL,
    accepted INTEGER NOT NULL DEFAULT 0,
    decision_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_family ON signals(family);

CREATE TABLE IF NOT EXISTS ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    bid REAL NOT NULL,
    ask REAL NOT NULL,
    last REAL,
    volume REAL,
    flags INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON ticks(symbol, ts);

CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    timeframe_minutes INTEGER NOT NULL,
    ts TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL DEFAULT 0,
    spread REAL,
    PRIMARY KEY (symbol, timeframe_minutes, ts)
);
"""


def _apply_migrations(con: sqlite3.Connection) -> None:
    cur = con.execute("PRAGMA user_version")
    current = int(cur.fetchone()[0])
    if current < 1:
        con.executescript(_SCHEMA_V1)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    # Future migrations: if current < 2: ...
    con.commit()


class StateDB:
    """Thin wrapper over a sqlite3.Connection with thread-safe access."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe because we serialize with a Lock;
        # WAL allows concurrent readers from other processes.
        self._con = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we use explicit transactions
            timeout=30.0,
        )
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode = WAL")
        self._con.execute("PRAGMA synchronous = NORMAL")
        self._con.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        _apply_migrations(self._con)

    # ------------------------------------------------------------------
    # primitives
    # ------------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic write transaction. Rollback on exception."""
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                yield self._con
                self._con.execute("COMMIT")
            except Exception:
                self._con.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._con.execute(sql, params)

    def executemany(self, sql: str, seq_of_params: list[tuple[Any, ...]]) -> sqlite3.Cursor:
        with self._lock:
            return self._con.executemany(sql, seq_of_params)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._con.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            row = self._con.execute(sql, params).fetchone()
            return row

    def close(self) -> None:
        with self._lock:
            self._con.close()

    # ------------------------------------------------------------------
    # convenience helpers (used by EventBus, ledger, agent-cycle)
    # ------------------------------------------------------------------
    def upsert_position(self, position: dict[str, Any]) -> None:
        cols = (
            "ticket symbol side units entry_price stop_price target_price "
            "opened_at closed_at closed_price pnl_dollars exit_reason magic "
            "family timeframe_minutes status"
        ).split()
        placeholders = ",".join("?" for _ in cols)
        col_list = ",".join(cols)
        update_set = ",".join(f"{c}=excluded.{c}" for c in cols if c != "ticket")
        sql = (
            f"INSERT INTO positions ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT(ticket) DO UPDATE SET {update_set}"
        )
        params = tuple(position.get(c) for c in cols)
        with self._lock:
            self._con.execute(sql, params)

    def upsert_pending_order(self, order: dict[str, Any]) -> None:
        cols = (
            "ticket symbol side units entry_price stop_price target_price "
            "placed_at cancelled_at magic status"
        ).split()
        placeholders = ",".join("?" for _ in cols)
        col_list = ",".join(cols)
        update_set = ",".join(f"{c}=excluded.{c}" for c in cols if c != "ticket")
        sql = (
            f"INSERT INTO pending_orders ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT(ticket) DO UPDATE SET {update_set}"
        )
        params = tuple(order.get(c) for c in cols)
        with self._lock:
            self._con.execute(sql, params)

    def insert_equity_snapshot(self, snap: dict[str, Any]) -> None:
        cols = (
            "ts broker_name equity balance margin_used margin_free "
            "paper_equity open_position_count pending_order_count"
        ).split()
        placeholders = ",".join("?" for _ in cols)
        col_list = ",".join(cols)
        sql = f"INSERT INTO equity_snapshots ({col_list}) VALUES ({placeholders})"
        params = tuple(snap.get(c) for c in cols)
        with self._lock:
            self._con.execute(sql, params)

    def insert_signal(self, sig: dict[str, Any]) -> int:
        cols = (
            "ts family timeframe_minutes side entry stop target score "
            "accepted decision_reason"
        ).split()
        placeholders = ",".join("?" for _ in cols)
        col_list = ",".join(cols)
        sql = f"INSERT INTO signals ({col_list}) VALUES ({placeholders})"
        params = tuple(sig.get(c) for c in cols)
        with self._lock:
            cur = self._con.execute(sql, params)
            return int(cur.lastrowid or 0)

    def upsert_fill(self, fill: dict[str, Any]) -> bool:
        """Insert a deal; returns True if new, False if duplicate."""
        cols = (
            "deal_id order_ticket position_ticket symbol side volume price "
            "profit swap commission fee deal_type entry_type reason time "
            "magic comment"
        ).split()
        placeholders = ",".join("?" for _ in cols)
        col_list = ",".join(cols)
        sql = (
            f"INSERT OR IGNORE INTO fills ({col_list}) VALUES ({placeholders})"
        )
        params = tuple(fill.get(c) for c in cols)
        with self._lock:
            cur = self._con.execute(sql, params)
            return cur.rowcount > 0


def open_state_db(path: Path | str = DEFAULT_DB_PATH) -> StateDB:
    """Open (and migrate) the state database. Creates parent dir."""
    return StateDB(path)
