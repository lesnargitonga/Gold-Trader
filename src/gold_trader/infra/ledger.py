"""Fills ledger: pulls broker deal history, dedupes, persists, emits events.

The ledger is the source of truth for closed trades.  Every cycle:

1. Read ``last_synced_time`` from the database (a row in a tiny
   ``ledger_state`` table, created lazily here).
2. Call ``broker.get_deals_since(last_synced_time)``.
3. For each deal, call ``StateDB.upsert_fill`` (INSERT OR IGNORE on
   ``deal_id``).  If the row was new, emit an event.
4. For ``DEAL_ENTRY_OUT`` (closing) deals, group by ``position_ticket``,
   sum profit + swap + commission + fee, and emit a single
   ``POSITION_CLOSED`` event with realised P&L per round-trip.
5. Update ``last_synced_time`` to the max deal time we just saw (or now if
   no deals).

The ledger is idempotent — running it twice is safe.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .events import EventBus, EventKind
from .db import StateDB
from .logging_config import get_logger
from ..live.broker import Broker, Deal, OrderSide

_log = get_logger("gold_trader.ledger")

_DEAL_ENTRY_IN = 0
_DEAL_ENTRY_OUT = 1
_DEAL_ENTRY_INOUT = 2  # net position flip


def _ensure_ledger_state_table(db: StateDB) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS ledger_state ("
        "  key TEXT PRIMARY KEY,"
        "  value TEXT NOT NULL"
        ")"
    )


def _read_last_synced(db: StateDB, magic: int) -> datetime:
    _ensure_ledger_state_table(db)
    row = db.query_one(
        "SELECT value FROM ledger_state WHERE key = ?",
        (f"last_synced_{magic}",),
    )
    if row is not None:
        try:
            return datetime.fromisoformat(row["value"])
        except (KeyError, ValueError, TypeError):
            pass
    # Default: 7 days ago.  First run pulls a week of history; cheap and
    # protects against missed deals from before the ledger existed.
    return datetime.now(timezone.utc) - timedelta(days=7)


def _write_last_synced(db: StateDB, magic: int, ts: datetime) -> None:
    _ensure_ledger_state_table(db)
    db.execute(
        "INSERT INTO ledger_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"last_synced_{magic}", ts.isoformat()),
    )


def sync_fills_ledger(
    broker: Broker,
    db: StateDB,
    bus: EventBus,
    *,
    magic: int = 20260507,
) -> dict[str, int]:
    """Pull new deals from the broker and persist them.

    Returns a dict with counts: ``new_deals``, ``new_round_trips``.
    """
    since = _read_last_synced(db, magic)
    try:
        deals = broker.get_deals_since(since, magic)
    except Exception as exc:  # broker may raise BrokerError; keep going
        _log.warning(
            "ledger_fetch_failed",
            extra={"error": str(exc), "since": since.isoformat()},
        )
        return {"new_deals": 0, "new_round_trips": 0}

    new_deals = 0
    closing_deals: dict[str, list[Deal]] = {}

    for d in deals:
        is_new = db.upsert_fill(_deal_to_row(d))
        if is_new:
            new_deals += 1
            bus.publish(
                EventKind.ORDER_FILLED,
                {
                    "deal_id": d.deal_id,
                    "order_ticket": d.order_ticket,
                    "position_ticket": d.position_ticket,
                    "symbol": d.symbol,
                    "side": d.side.value,
                    "volume": d.volume,
                    "price": d.price,
                    "profit": d.profit,
                    "entry_type": d.entry_type,
                    "time": d.time.isoformat(),
                    "magic": d.magic,
                },
            )
        if d.entry_type == _DEAL_ENTRY_OUT and d.position_ticket:
            closing_deals.setdefault(d.position_ticket, []).append(d)

    # Emit one POSITION_CLOSED per round-trip.
    new_round_trips = 0
    for position_ticket, group in closing_deals.items():
        # All deals in this group are out-legs; sum P&L.  We also look up
        # whether we've already announced this round-trip by checking the
        # positions table: if status is already 'closed', skip.
        prior = db.query_one(
            "SELECT status FROM positions WHERE ticket = ?",
            (position_ticket,),
        )
        if prior is not None and prior["status"] == "closed":
            continue

        pnl = sum(d.profit + d.swap + d.commission + d.fee for d in group)
        last = max(group, key=lambda d: d.time)
        # Mirror into positions table (status -> closed) so the dashboard
        # can render it; we don't always have full open-side context here.
        db.execute(
            "UPDATE positions SET status='closed', closed_at=?, "
            "closed_price=?, pnl_dollars=?, exit_reason=COALESCE(exit_reason,'broker') "
            "WHERE ticket=?",
            (last.time.isoformat(), last.price, pnl, position_ticket),
        )
        bus.publish(
            EventKind.POSITION_CLOSED,
            {
                "position_ticket": position_ticket,
                "exit_price": last.price,
                "exit_time": last.time.isoformat(),
                "pnl_dollars": pnl,
                "deal_count": len(group),
                "exit_side": last.side.value,
            },
        )
        new_round_trips += 1

    # Advance watermark to the most-recent deal we saw, falling back to
    # the start of *now* minus 1s to avoid rewinding past unseen deals.
    if deals:
        watermark = max(d.time for d in deals)
        # Bump 1ms forward so we don't keep re-fetching the same boundary deal.
        watermark = watermark + timedelta(milliseconds=1)
    else:
        watermark = since
    _write_last_synced(db, magic, watermark)
    return {"new_deals": new_deals, "new_round_trips": new_round_trips}


def _deal_to_row(d: Deal) -> dict[str, object]:
    return {
        "deal_id": d.deal_id,
        "order_ticket": d.order_ticket,
        "position_ticket": d.position_ticket,
        "symbol": d.symbol,
        "side": d.side.value if isinstance(d.side, OrderSide) else d.side,
        "volume": d.volume,
        "price": d.price,
        "profit": d.profit,
        "swap": d.swap,
        "commission": d.commission,
        "fee": d.fee,
        "deal_type": d.deal_type,
        "entry_type": d.entry_type,
        "reason": d.reason,
        "time": d.time.isoformat(),
        "magic": d.magic,
        "comment": d.comment,
    }
