"""Event bus with durable persistence (SQLite + JSONL mirror).

Every important moment in the live agent emits an :class:`Event`.  Events are:

1. Persisted to the ``events`` table in SQLite (queryable history).
2. Mirrored to ``logs/events.jsonl`` (tail-friendly, log-shipper friendly).
3. Broadcast to in-process subscribers (synchronous; subscribers should be
   fast or queue work themselves).
4. Logged via the structured ``events`` logger.

Event kinds are an enum so typos surface at import time.

Schema of an event payload is intentionally loose (free-form dict) — the
event kind defines the contract.  Kinds use snake_case past-tense verbs.
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .db import StateDB
from .logging_config import get_logger


_log = get_logger("gold_trader.events")


class EventKind(str, Enum):
    AGENT_CYCLE_STARTED = "agent_cycle_started"
    AGENT_CYCLE_FINISHED = "agent_cycle_finished"
    SIGNAL_EMITTED = "signal_emitted"
    DECISION_MADE = "decision_made"
    ORDER_PLACED = "order_placed"
    ORDER_REJECTED = "order_rejected"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    PAPER_POSITION_OPENED = "paper_position_opened"
    PAPER_POSITION_CLOSED = "paper_position_closed"
    KILL_SWITCH_TRIGGERED = "kill_switch_triggered"
    BRIDGE_ERROR = "bridge_error"
    LIVE_RECONCILE = "live_reconcile"
    EQUITY_SNAPSHOT = "equity_snapshot"


@dataclass(frozen=True)
class Event:
    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    correlation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "kind": self.kind.value,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
        }


Subscriber = Callable[[Event], None]


class EventBus:
    """Synchronous event bus with SQLite + JSONL persistence.

    Thread-safe.  ``publish()`` returns only after the event is durably
    written and all subscribers have been notified.
    """

    def __init__(
        self,
        db: StateDB,
        jsonl_path: Path | str | None = Path("logs/events.jsonl"),
    ) -> None:
        self._db = db
        self._jsonl_path: Path | None = (
            Path(jsonl_path) if jsonl_path is not None else None
        )
        if self._jsonl_path is not None:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._subscribers: list[Subscriber] = []
        self._lock = threading.RLock()

    def subscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.append(subscriber)

    def new_correlation_id(self) -> str:
        return uuid.uuid4().hex

    def publish(
        self,
        kind: EventKind,
        payload: dict[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
    ) -> Event:
        event = Event(
            kind=kind,
            payload=dict(payload or {}),
            correlation_id=correlation_id,
        )
        with self._lock:
            # 1) Durable SQL row.
            try:
                self._db.execute(
                    "INSERT INTO events (ts, kind, payload_json, correlation_id) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        event.ts,
                        event.kind.value,
                        json.dumps(event.payload, default=str),
                        event.correlation_id,
                    ),
                )
            except Exception:
                _log.exception("event_db_insert_failed", extra={"kind": kind.value})
            # 2) JSONL mirror.
            if self._jsonl_path is not None:
                try:
                    line = json.dumps(event.to_dict(), default=str)
                    with self._jsonl_path.open("a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except Exception:
                    _log.exception(
                        "event_jsonl_append_failed", extra={"kind": kind.value}
                    )
            # 3) Structured log line.
            _log.info(
                event.kind.value,
                extra={
                    "correlation_id": event.correlation_id,
                    **event.payload,
                },
            )
            # 4) In-process subscribers (best-effort; one bad sub does not
            #    break the rest).
            for sub in list(self._subscribers):
                try:
                    sub(event)
                except Exception:
                    _log.exception(
                        "event_subscriber_failed",
                        extra={"kind": kind.value, "subscriber": repr(sub)},
                    )
        return event
