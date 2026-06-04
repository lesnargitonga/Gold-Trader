"""Research position state machine.

Provides a lightweight ``PositionState`` dataclass and persistence helpers
for tracking open research/live positions across agent-cycle invocations.

This is intentionally independent of the ``PaperState`` paper-trading state
so that research scripts can use it without pulling in paper-trading concerns.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ..models import MarketBar, Side


@dataclass
class PositionState:
    """A single tracked position."""

    opened_at: datetime         # UTC timestamp when the signal was generated
    family: str                 # strategy family name
    timeframe_minutes: int      # bar timeframe that generated the signal
    side: Side                  # LONG or SHORT
    entry: float                # assumed entry price
    stop: float                 # initial stop-loss level
    target: float               # take-profit level
    bars_held: int = 0          # bars elapsed since entry
    max_hold_bars: int = 24     # force-close after this many bars
    status: Literal["open", "closed"] = "open"
    exit_price: float | None = None
    exit_reason: str | None = None

    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["opened_at"] = self.opened_at.isoformat()
        d["side"] = self.side.value if hasattr(self.side, "value") else str(self.side)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PositionState":
        d = dict(d)
        d["opened_at"] = datetime.fromisoformat(d["opened_at"])
        if not d["opened_at"].tzinfo:
            d["opened_at"] = d["opened_at"].replace(tzinfo=timezone.utc)
        d["side"] = Side[d["side"].upper()] if isinstance(d["side"], str) else d["side"]
        return cls(**d)


# ── Persistence ─────────────────────────────────────────────────────────────

def load_positions(path: str | Path) -> list[PositionState]:
    """Load position list from *path*.  Returns empty list if file doesn't exist."""
    p = Path(path)
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    return [PositionState.from_dict(item) for item in raw]


def save_positions(positions: list[PositionState], path: str | Path) -> None:
    """Persist *positions* to *path* as JSON."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump([pos.to_dict() for pos in positions], fh, indent=2)


# ── State transitions ────────────────────────────────────────────────────────

def check_position_exit(
    position: PositionState,
    bar: MarketBar,
) -> tuple[float, str] | tuple[None, None]:
    """Check whether *bar* triggers an exit for *position*.

    Tests (in order):
    1. Stop-loss: bar traded through the stop level.
    2. Take-profit: bar traded through the target level.
    3. Time stop: ``bars_held >= max_hold_bars``.

    Returns ``(exit_price, reason)`` if an exit is triggered, else ``(None, None)``.
    """
    if position.side == Side.LONG:
        if bar.low <= position.stop:
            return position.stop, "stop_loss"
        if bar.high >= position.target:
            return position.target, "take_profit"
    else:  # SHORT
        if bar.high >= position.stop:
            return position.stop, "stop_loss"
        if bar.low <= position.target:
            return position.target, "take_profit"

    if position.bars_held >= position.max_hold_bars:
        return bar.close, "time_exit"

    return None, None


def update_position(position: PositionState, bar: MarketBar) -> PositionState:
    """Return a copy of *position* with ``bars_held`` incremented by 1.

    Call this once per bar when no exit has been triggered to advance the
    time-stop counter.
    """
    return PositionState(
        opened_at=position.opened_at,
        family=position.family,
        timeframe_minutes=position.timeframe_minutes,
        side=position.side,
        entry=position.entry,
        stop=position.stop,
        target=position.target,
        bars_held=position.bars_held + 1,
        max_hold_bars=position.max_hold_bars,
        status=position.status,
        exit_price=position.exit_price,
        exit_reason=position.exit_reason,
    )


def close_position(
    position: PositionState,
    exit_price: float,
    exit_reason: str,
) -> PositionState:
    """Return a copy of *position* marked as closed with *exit_price* and *exit_reason*."""
    return PositionState(
        opened_at=position.opened_at,
        family=position.family,
        timeframe_minutes=position.timeframe_minutes,
        side=position.side,
        entry=position.entry,
        stop=position.stop,
        target=position.target,
        bars_held=position.bars_held,
        max_hold_bars=position.max_hold_bars,
        status="closed",
        exit_price=exit_price,
        exit_reason=exit_reason,
    )
