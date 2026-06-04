"""Risk hardening: equity-guard kill switch + broker-vs-paper divergence guard.

Phase D1 — equity guard
-----------------------
``evaluate_equity_guard`` looks at broker-side equity vs balance, computes
intra-day drawdown, and returns a structured verdict.  When the verdict is
``trip``, ``trip_kill_switch`` flattens the broker (cancels every pending
under our magic, closes every open position under our magic) and emits
``KILL_SWITCH_TRIGGERED`` through the event bus.

The verdict input is broker truth — never the paper sim.  This is by design:
if the bridge drops a fill or our internal state desyncs, the broker's
account_info is what actually matters.

Phase D2 — divergence guard
---------------------------
``evaluate_divergence_guard`` reads the realised P&L stored in the ``fills``
table (sum of profit + swap + commission + fee for our magic), compares it to
the broker's reported balance delta since the day's starting point, and to
the paper-sim equity delta.  Divergence above a configurable threshold
returns a verdict suitable for emitting an alert event.

Both guards are pure functions on top of small reads — safe to call every
cycle and safe to call from a separate watchdog process.

Tested via tests/test_risk.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .db import StateDB
from .events import EventBus, EventKind
from .logging_config import get_logger
from ..live.broker import Broker, BrokerError

_log = get_logger("gold_trader.risk")


# ---------------------------------------------------------------------------
# D1 — equity guard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EquityGuardConfig:
    """Hard limits on broker-side equity.

    All values are inclusive thresholds on *negative* drawdown — i.e. the
    guard trips when the loss meets or exceeds the limit.
    """

    # Maximum acceptable intra-day loss as a fraction of session-start equity.
    # 0.04 = 4% drawdown trips.
    daily_loss_fraction: float = 0.04
    # Absolute floor on equity.  If equity falls below this, trip immediately.
    # 0 disables; sensible to set near broker margin call.
    min_absolute_equity: float = 0.0
    # Maximum acceptable single-cycle equity drop (catches catastrophic
    # slippage events even when daily PnL is still positive).
    max_single_cycle_loss_fraction: float = 0.025


@dataclass(frozen=True)
class EquityGuardVerdict:
    decision: str  # "ok" | "warn" | "trip"
    reason: str
    equity: float
    balance: float
    session_start_equity: float
    daily_pnl: float
    daily_pnl_fraction: float
    triggered_rule: str | None = None


def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _read_session_start_equity(db: StateDB, today_iso: str) -> float | None:
    """First equity_snapshots row on or after the start of today (UTC)."""
    row = db.query_one(
        "SELECT equity FROM equity_snapshots "
        "WHERE substr(ts, 1, 10) = ? "
        "ORDER BY ts ASC LIMIT 1",
        (today_iso,),
    )
    if row is None:
        return None
    try:
        return float(row["equity"])
    except (KeyError, TypeError, ValueError):
        return None


def _read_previous_equity(db: StateDB) -> float | None:
    """Most recent equity_snapshots row (regardless of day)."""
    row = db.query_one(
        "SELECT equity FROM equity_snapshots ORDER BY id DESC LIMIT 1",
    )
    if row is None:
        return None
    try:
        return float(row["equity"])
    except (KeyError, TypeError, ValueError):
        return None


def evaluate_equity_guard(
    broker: Broker,
    db: StateDB,
    *,
    config: EquityGuardConfig | None = None,
) -> EquityGuardVerdict:
    """Compute the equity-guard verdict for the current broker state.

    Pure function.  Does not modify state, does not place broker calls
    other than ``get_account_info``.
    """
    cfg = config or EquityGuardConfig()
    info = broker.get_account_info()
    equity = float(info.equity)
    balance = float(info.balance)
    today = _today_utc()

    session_start = _read_session_start_equity(db, today) or balance
    daily_pnl = equity - session_start
    daily_pnl_fraction = daily_pnl / session_start if session_start else 0.0

    # 1) Absolute floor.
    if cfg.min_absolute_equity > 0 and equity <= cfg.min_absolute_equity:
        return EquityGuardVerdict(
            decision="trip",
            reason=f"equity {equity:.2f} below absolute floor {cfg.min_absolute_equity:.2f}",
            equity=equity,
            balance=balance,
            session_start_equity=session_start,
            daily_pnl=daily_pnl,
            daily_pnl_fraction=daily_pnl_fraction,
            triggered_rule="min_absolute_equity",
        )

    # 2) Daily drawdown.
    if daily_pnl_fraction <= -abs(cfg.daily_loss_fraction):
        return EquityGuardVerdict(
            decision="trip",
            reason=(
                f"daily drawdown {daily_pnl_fraction*100:.2f}% breaches "
                f"{cfg.daily_loss_fraction*100:.2f}% limit"
            ),
            equity=equity,
            balance=balance,
            session_start_equity=session_start,
            daily_pnl=daily_pnl,
            daily_pnl_fraction=daily_pnl_fraction,
            triggered_rule="daily_loss_fraction",
        )

    # 3) Single-cycle catastrophic drop (vs last snapshot regardless of day).
    last_equity = _read_previous_equity(db)
    if last_equity is not None and last_equity > 0:
        single = (equity - last_equity) / last_equity
        if single <= -abs(cfg.max_single_cycle_loss_fraction):
            return EquityGuardVerdict(
                decision="trip",
                reason=(
                    f"single-cycle drop {single*100:.2f}% breaches "
                    f"{cfg.max_single_cycle_loss_fraction*100:.2f}% limit"
                ),
                equity=equity,
                balance=balance,
                session_start_equity=session_start,
                daily_pnl=daily_pnl,
                daily_pnl_fraction=daily_pnl_fraction,
                triggered_rule="max_single_cycle_loss_fraction",
            )

    # 4) Warn if within 80% of daily limit.
    if daily_pnl_fraction <= -0.8 * abs(cfg.daily_loss_fraction):
        return EquityGuardVerdict(
            decision="warn",
            reason=(
                f"daily drawdown {daily_pnl_fraction*100:.2f}% within 80% of limit"
            ),
            equity=equity,
            balance=balance,
            session_start_equity=session_start,
            daily_pnl=daily_pnl,
            daily_pnl_fraction=daily_pnl_fraction,
            triggered_rule=None,
        )

    return EquityGuardVerdict(
        decision="ok",
        reason="within limits",
        equity=equity,
        balance=balance,
        session_start_equity=session_start,
        daily_pnl=daily_pnl,
        daily_pnl_fraction=daily_pnl_fraction,
    )


@dataclass
class FlattenReport:
    cancelled_pending: list[str] = field(default_factory=list)
    closed_positions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def flatten_account(
    broker: Broker,
    *,
    magic: int,
    reason: str,
) -> FlattenReport:
    """Cancel every pending and close every open position for this magic.

    Best-effort; collects errors, never raises.  Returns a report so the
    caller can include it in the kill-switch event payload.
    """
    rep = FlattenReport()
    # Cancel any resting pending order first to avoid filling during close.
    try:
        pending = broker.get_pending_order(magic)
    except BrokerError as exc:
        rep.errors.append(f"get_pending_order: {exc}")
        pending = None
    if pending is not None:
        try:
            ok = broker.cancel_pending_order(pending.broker_order_id)
            if ok:
                rep.cancelled_pending.append(pending.broker_order_id)
            else:
                rep.errors.append(
                    f"cancel_pending_order returned False for {pending.broker_order_id}"
                )
        except BrokerError as exc:
            rep.errors.append(f"cancel_pending_order: {exc}")

    # Close open position.
    try:
        position = broker.get_open_position(magic)
    except BrokerError as exc:
        rep.errors.append(f"get_open_position: {exc}")
        position = None
    if position is not None:
        try:
            closed = broker.close_position(position.broker_order_id, reason=reason)
            if closed is not None:
                rep.closed_positions.append(position.broker_order_id)
            else:
                rep.errors.append(
                    f"close_position returned None for {position.broker_order_id}"
                )
        except BrokerError as exc:
            rep.errors.append(f"close_position: {exc}")

    return rep


def trip_kill_switch(
    broker: Broker,
    db: StateDB,
    bus: EventBus,
    verdict: EquityGuardVerdict,
    *,
    magic: int,
    correlation_id: str | None = None,
) -> FlattenReport:
    """Flatten the account and emit ``KILL_SWITCH_TRIGGERED``.

    Idempotent at the event level: if a kill-switch event already exists
    for today the function still flattens (defensive — flattening twice
    is a no-op when already flat) but the second event is published with
    ``duplicate=True`` so log readers can dedupe.
    """
    today = _today_utc()
    duplicate = False
    row = db.query_one(
        "SELECT 1 FROM events "
        "WHERE kind = ? AND substr(ts, 1, 10) = ? LIMIT 1",
        (EventKind.KILL_SWITCH_TRIGGERED.value, today),
    )
    if row is not None:
        duplicate = True

    report = flatten_account(broker, magic=magic, reason="kill_switch")
    payload: dict[str, Any] = {
        "rule": verdict.triggered_rule,
        "reason": verdict.reason,
        "equity": verdict.equity,
        "balance": verdict.balance,
        "session_start_equity": verdict.session_start_equity,
        "daily_pnl": verdict.daily_pnl,
        "daily_pnl_fraction": verdict.daily_pnl_fraction,
        "cancelled_pending": list(report.cancelled_pending),
        "closed_positions": list(report.closed_positions),
        "errors": list(report.errors),
        "magic": magic,
        "duplicate": duplicate,
    }
    bus.publish(
        EventKind.KILL_SWITCH_TRIGGERED,
        payload,
        correlation_id=correlation_id,
    )
    _log.warning(
        "kill_switch_tripped",
        extra={
            "rule": verdict.triggered_rule,
            "equity": verdict.equity,
            "daily_pnl_fraction": verdict.daily_pnl_fraction,
        },
    )
    return report


# ---------------------------------------------------------------------------
# B4 — connectivity / tick-age watchdog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickAgeVerdict:
    decision: str  # "ok" | "stale" | "missing"
    last_tick_age_sec: float | None
    threshold_sec: float
    reason: str


def evaluate_tick_age(
    last_tick_age_sec: float | None,
    *,
    threshold_sec: float = 300.0,
) -> TickAgeVerdict:
    """Pure check — feed it the value reported by the bridge ``/healthz``.

    ``None`` means "no tick ever observed", which is treated as ``missing``.
    """
    if last_tick_age_sec is None:
        return TickAgeVerdict(
            decision="missing",
            last_tick_age_sec=None,
            threshold_sec=threshold_sec,
            reason="no tick observed since bridge start",
        )
    if last_tick_age_sec > threshold_sec:
        return TickAgeVerdict(
            decision="stale",
            last_tick_age_sec=last_tick_age_sec,
            threshold_sec=threshold_sec,
            reason=(
                f"last tick is {last_tick_age_sec:.0f}s old "
                f"(threshold {threshold_sec:.0f}s)"
            ),
        )
    return TickAgeVerdict(
        decision="ok",
        last_tick_age_sec=last_tick_age_sec,
        threshold_sec=threshold_sec,
        reason="fresh",
    )


# ---------------------------------------------------------------------------
# D2 — divergence guard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DivergenceConfig:
    """Tolerance for paper vs broker realised-P&L drift."""

    # Absolute dollar tolerance below which divergence is ignored.
    absolute_tolerance: float = 5.0
    # Fractional tolerance relative to the larger of |broker_pnl| or
    # |paper_pnl|.  0.005 = 0.5% drift allowed.
    relative_tolerance: float = 0.005


@dataclass(frozen=True)
class DivergenceVerdict:
    decision: str  # "ok" | "warn" | "alert"
    broker_realised_pnl: float
    ledger_realised_pnl: float
    paper_pnl: float
    broker_minus_ledger: float
    broker_minus_paper: float
    reason: str


def _ledger_realised_pnl(db: StateDB, magic: int, since_iso: str | None) -> float:
    """Sum profit + swap + commission + fee from fills for our magic."""
    if since_iso:
        rows = db.query(
            "SELECT profit, swap, commission, fee FROM fills "
            "WHERE magic = ? AND time >= ?",
            (magic, since_iso),
        )
    else:
        rows = db.query(
            "SELECT profit, swap, commission, fee FROM fills WHERE magic = ?",
            (magic,),
        )
    return sum(
        float(r["profit"]) + float(r["swap"])
        + float(r["commission"]) + float(r["fee"])
        for r in rows
    )


def evaluate_divergence_guard(
    broker: Broker,
    db: StateDB,
    *,
    paper_equity: float,
    paper_starting_equity: float,
    magic: int,
    config: DivergenceConfig | None = None,
    since_iso: str | None = None,
) -> DivergenceVerdict:
    """Compare broker-realised P&L vs ledger-realised P&L vs paper sim.

    Three numbers should agree (within tolerance) once trades have
    actually been realised:

    1. ``broker.balance - session_start_balance`` (the truth from MT5).
    2. ``sum(fills)`` for our magic since session start (our ledger).
    3. ``paper_equity - paper_starting_equity`` (the simulator).
    """
    cfg = config or DivergenceConfig()
    info = broker.get_account_info()
    today = _today_utc()
    # Use the first equity_snapshots balance of the day as session start.
    row = db.query_one(
        "SELECT balance FROM equity_snapshots "
        "WHERE substr(ts, 1, 10) = ? ORDER BY ts ASC LIMIT 1",
        (today,),
    )
    session_start_balance = float(row["balance"]) if row else float(info.balance)
    broker_realised = float(info.balance) - session_start_balance
    ledger_realised = _ledger_realised_pnl(db, magic, since_iso or today)
    paper_pnl = float(paper_equity) - float(paper_starting_equity)

    broker_minus_ledger = broker_realised - ledger_realised
    broker_minus_paper = broker_realised - paper_pnl

    def _is_divergent(delta: float, a: float, b: float) -> bool:
        if abs(delta) <= cfg.absolute_tolerance:
            return False
        scale = max(abs(a), abs(b), 1.0)
        return abs(delta) / scale > cfg.relative_tolerance

    bl_div = _is_divergent(broker_minus_ledger, broker_realised, ledger_realised)
    bp_div = _is_divergent(broker_minus_paper, broker_realised, paper_pnl)

    if bl_div:
        return DivergenceVerdict(
            decision="alert",
            broker_realised_pnl=broker_realised,
            ledger_realised_pnl=ledger_realised,
            paper_pnl=paper_pnl,
            broker_minus_ledger=broker_minus_ledger,
            broker_minus_paper=broker_minus_paper,
            reason=(
                f"broker realised {broker_realised:+.2f} differs from "
                f"ledger {ledger_realised:+.2f} by {broker_minus_ledger:+.2f}"
            ),
        )
    if bp_div:
        return DivergenceVerdict(
            decision="warn",
            broker_realised_pnl=broker_realised,
            ledger_realised_pnl=ledger_realised,
            paper_pnl=paper_pnl,
            broker_minus_ledger=broker_minus_ledger,
            broker_minus_paper=broker_minus_paper,
            reason=(
                f"broker {broker_realised:+.2f} vs paper {paper_pnl:+.2f} "
                f"diverge by {broker_minus_paper:+.2f}"
            ),
        )
    return DivergenceVerdict(
        decision="ok",
        broker_realised_pnl=broker_realised,
        ledger_realised_pnl=ledger_realised,
        paper_pnl=paper_pnl,
        broker_minus_ledger=broker_minus_ledger,
        broker_minus_paper=broker_minus_paper,
        reason="within tolerance",
    )
