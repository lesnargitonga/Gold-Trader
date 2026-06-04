from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ..models import MarketBar, Side
from ..research.state import DecisionPlan


@dataclass
class PaperPosition:
    opened_at: str
    family: str
    timeframe_minutes: int
    side: str
    entry: float
    stop: float
    target: float
    status: str = "open"
    closed_at: str | None = None
    closed_price: float | None = None
    pnl_r: float | None = None
    exit_reason: str | None = None


@dataclass
class PaperState:
    open_position: PaperPosition | None
    closed_positions: list[PaperPosition]
    paper_equity: float
    daily_peak_equity: float
    last_updated: str
    total_trades: int
    winning_trades: int
    daily_reset_date: str | None = None  # ISO date string of the last daily reset
    daily_trades_opened: int = 0  # trades opened on daily_reset_date

    @property
    def win_rate(self) -> float:
        return self.winning_trades / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def kill_switch_triggered(self) -> bool:
        return self.paper_equity <= self.daily_peak_equity * 0.96

    def with_daily_reset_if_needed(self) -> "PaperState":
        """Return a new PaperState with daily_peak_equity reset if we've rolled to a new UTC day."""
        today = datetime.now(timezone.utc).date().isoformat()
        if self.daily_reset_date == today:
            return self
        return PaperState(
            open_position=self.open_position,
            closed_positions=self.closed_positions,
            paper_equity=self.paper_equity,
            daily_peak_equity=self.paper_equity,  # reset peak to current equity
            last_updated=self.last_updated,
            total_trades=self.total_trades,
            winning_trades=self.winning_trades,
            daily_reset_date=today,
            daily_trades_opened=0,  # reset daily trade counter
        )

    def to_dict(self) -> dict:
        return {
            "open_position": asdict(self.open_position) if self.open_position else None,
            "closed_positions": [asdict(p) for p in self.closed_positions],
            "paper_equity": self.paper_equity,
            "daily_peak_equity": self.daily_peak_equity,
            "last_updated": self.last_updated,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "daily_reset_date": self.daily_reset_date,
            "daily_trades_opened": self.daily_trades_opened,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PaperState:
        op = data.get("open_position")
        return cls(
            open_position=PaperPosition(**op) if op else None,
            closed_positions=[PaperPosition(**p) for p in data.get("closed_positions", [])],
            paper_equity=float(data.get("paper_equity", 10_000.0)),
            daily_peak_equity=float(data.get("daily_peak_equity", 10_000.0)),
            last_updated=data.get("last_updated", _utc_now_str()),
            total_trades=int(data.get("total_trades", 0)),
            winning_trades=int(data.get("winning_trades", 0)),
            daily_reset_date=data.get("daily_reset_date"),
            daily_trades_opened=int(data.get("daily_trades_opened", 0)),
        )


def load_paper_state(path: Path, starting_equity: float = 10_000.0) -> PaperState:
    if not path.exists():
        return PaperState(
            open_position=None,
            closed_positions=[],
            paper_equity=starting_equity,
            daily_peak_equity=starting_equity,
            last_updated=_utc_now_str(),
            total_trades=0,
            winning_trades=0,
        )
    with path.open("r", encoding="utf-8") as f:
        return PaperState.from_dict(json.load(f))


def save_paper_state(state: PaperState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2)


def open_position_from_decision(decision: DecisionPlan, bars_snapshot: Sequence[MarketBar]) -> PaperPosition | None:
    """Create a PaperPosition from an accepted decision.  Returns None if the
    decision is not an accept or is missing required fields."""
    if decision.status != "accept":
        return None
    if None in (decision.family, decision.timeframe_minutes, decision.side,
                decision.reference_price, decision.stop, decision.target):
        return None
    return PaperPosition(
        opened_at=_utc_now_str(),
        family=decision.family,  # type: ignore[arg-type]
        timeframe_minutes=decision.timeframe_minutes,  # type: ignore[arg-type]
        side=decision.side.value,  # type: ignore[union-attr]
        entry=decision.reference_price,  # type: ignore[arg-type]
        stop=decision.stop,  # type: ignore[arg-type]
        target=decision.target,  # type: ignore[arg-type]
    )


def monitor_open_position(
    state: PaperState,
    bars: Sequence[MarketBar],
    risk_per_trade: float = 0.01,
) -> tuple[PaperState, str | None]:
    """Check the most recent bars against the open position's stop and target.

    Returns (updated_state, event_message).  *event_message* is None if the
    position is still open, or a human-readable string describing the close event.
    """
    pos = state.open_position
    if pos is None:
        return state, None

    side = Side(pos.side)
    opened_at_dt = datetime.fromisoformat(pos.opened_at)
    if opened_at_dt.tzinfo is None:
        opened_at_dt = opened_at_dt.replace(tzinfo=timezone.utc)

    exit_price: float | None = None
    exit_reason: str | None = None
    exit_time: str | None = None

    for bar in bars:
        bar_time = bar.timestamp
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=timezone.utc)
        if bar_time <= opened_at_dt:
            continue

        if side is Side.LONG:
            if bar.low <= pos.stop:
                exit_price = pos.stop
                exit_reason = "stop"
                exit_time = bar_time.isoformat()
                break
            if bar.high >= pos.target:
                exit_price = pos.target
                exit_reason = "target"
                exit_time = bar_time.isoformat()
                break
        else:
            if bar.high >= pos.stop:
                exit_price = pos.stop
                exit_reason = "stop"
                exit_time = bar_time.isoformat()
                break
            if bar.low <= pos.target:
                exit_price = pos.target
                exit_reason = "target"
                exit_time = bar_time.isoformat()
                break

    if exit_price is None:
        return state, None

    price_move = (exit_price - pos.entry) * (1 if side is Side.LONG else -1)
    risk = abs(pos.entry - pos.stop)
    pnl_r = price_move / risk if risk > 0 else 0.0

    risk_dollars = state.paper_equity * risk_per_trade
    pnl_dollars = pnl_r * risk_dollars

    closed = PaperPosition(
        opened_at=pos.opened_at,
        family=pos.family,
        timeframe_minutes=pos.timeframe_minutes,
        side=pos.side,
        entry=pos.entry,
        stop=pos.stop,
        target=pos.target,
        status=f"closed_{exit_reason}",
        closed_at=exit_time,
        closed_price=exit_price,
        pnl_r=round(pnl_r, 4),
        exit_reason=exit_reason,
    )

    new_equity = state.paper_equity + pnl_dollars
    new_peak = max(state.daily_peak_equity, new_equity)
    new_winning = state.winning_trades + (1 if pnl_r > 0 else 0)

    new_state = PaperState(
        open_position=None,
        closed_positions=state.closed_positions + [closed],
        paper_equity=round(new_equity, 2),
        daily_peak_equity=round(new_peak, 2),
        last_updated=_utc_now_str(),
        total_trades=state.total_trades + 1,
        winning_trades=new_winning,
        daily_reset_date=state.daily_reset_date,
        daily_trades_opened=state.daily_trades_opened,
    )

    msg = (
        f"position_closed: {exit_reason.upper()} "
        f"side={pos.side} entry={pos.entry:.2f} exit={exit_price:.2f} "
        f"pnl_r={pnl_r:+.3f} pnl_usd={pnl_dollars:+.2f} equity={new_equity:.2f}"
    )
    return new_state, msg


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def force_close_open_position(
    state: PaperState,
    exit_price: float,
    reason: str = "live_reconcile",
    risk_per_trade: float = 0.01,
) -> tuple[PaperState, str | None]:
    """Force-close the paper open position at *exit_price*.

    Used when the live broker is the source of truth and reports flat while
    the paper sim still shows an open position (e.g. SL/TP hit on broker side
    or a manual close).  Returns (updated_state, event_message); message is
    None if there was nothing to close.
    """
    pos = state.open_position
    if pos is None:
        return state, None

    side = Side(pos.side)
    price_move = (exit_price - pos.entry) * (1 if side is Side.LONG else -1)
    risk = abs(pos.entry - pos.stop)
    pnl_r = price_move / risk if risk > 0 else 0.0
    risk_dollars = state.paper_equity * risk_per_trade
    pnl_dollars = pnl_r * risk_dollars

    closed = PaperPosition(
        opened_at=pos.opened_at,
        family=pos.family,
        timeframe_minutes=pos.timeframe_minutes,
        side=pos.side,
        entry=pos.entry,
        stop=pos.stop,
        target=pos.target,
        status=f"closed_{reason}",
        closed_at=_utc_now_str(),
        closed_price=exit_price,
        pnl_r=round(pnl_r, 4),
        exit_reason=reason,
    )
    new_equity = state.paper_equity + pnl_dollars
    new_peak = max(state.daily_peak_equity, new_equity)
    new_winning = state.winning_trades + (1 if pnl_r > 0 else 0)

    new_state = PaperState(
        open_position=None,
        closed_positions=state.closed_positions + [closed],
        paper_equity=round(new_equity, 2),
        daily_peak_equity=round(new_peak, 2),
        last_updated=_utc_now_str(),
        total_trades=state.total_trades + 1,
        winning_trades=new_winning,
        daily_reset_date=state.daily_reset_date,
        daily_trades_opened=state.daily_trades_opened,
    )
    msg = (
        f"position_closed: {reason.upper()} "
        f"side={pos.side} entry={pos.entry:.2f} exit={exit_price:.2f} "
        f"pnl_r={pnl_r:+.3f} pnl_usd={pnl_dollars:+.2f} equity={new_equity:.2f}"
    )
    return new_state, msg
