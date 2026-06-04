"""Tiny persistent state file for the live agent-cycle path.

Tracks daily trade counts and a rolling ledger of closed trades observed
through the broker.  Deliberately separate from `PaperState` so live and
paper flows can coexist in the same workspace without clobbering each other.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from pathlib import Path
import json

from .broker import ClosedTrade, OrderSide


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


@dataclass
class LiveAgentState:
    """Persisted across agent-cycle invocations."""

    daily_reset_date: date = field(default_factory=_utc_today)
    daily_trades_opened: int = 0
    last_known_position_id: str | None = None
    closed_trades: list[dict] = field(default_factory=list)  # serialized ClosedTrade

    def with_daily_reset_if_needed(self) -> "LiveAgentState":
        today = _utc_today()
        if today != self.daily_reset_date:
            return LiveAgentState(
                daily_reset_date=today,
                daily_trades_opened=0,
                last_known_position_id=self.last_known_position_id,
                closed_trades=self.closed_trades,
            )
        return self


def load_live_state(path: Path) -> LiveAgentState:
    if not path.exists():
        return LiveAgentState()
    raw = json.loads(path.read_text())
    return LiveAgentState(
        daily_reset_date=date.fromisoformat(raw["daily_reset_date"]),
        daily_trades_opened=int(raw.get("daily_trades_opened", 0)),
        last_known_position_id=raw.get("last_known_position_id"),
        closed_trades=list(raw.get("closed_trades", [])),
    )


def save_live_state(state: LiveAgentState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "daily_reset_date": state.daily_reset_date.isoformat(),
        "daily_trades_opened": state.daily_trades_opened,
        "last_known_position_id": state.last_known_position_id,
        "closed_trades": state.closed_trades[-200:],  # cap ledger
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def serialize_closed_trade(t: ClosedTrade) -> dict:
    return {
        "broker_order_id": t.broker_order_id,
        "symbol": t.symbol,
        "side": t.side.value if isinstance(t.side, OrderSide) else str(t.side),
        "units": t.units,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "pnl_dollars": t.pnl_dollars,
        "exit_reason": t.exit_reason,
    }
