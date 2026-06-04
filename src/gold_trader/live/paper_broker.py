"""Paper broker — wraps the existing PaperState behind the Broker interface.

This lets the agent-cycle program against the same abstraction whether it's
trading paper or real.  The wrapper does not change paper-trade semantics:
all P&L is still simulated against historical bars by ``monitor_open_position``
in the existing paper module.

What this wrapper does NOT do
-----------------------------
* Fill simulation against live bars (the engine does this for backtests).
* Real-time pricing.  ``place_market_order`` records the entry at the price
  the caller supplied — exactly mirroring the existing paper flow.

This wrapper exists so the agent-cycle can be written against ``Broker`` and
swapped to MT5 by changing one env var.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ..models import Side
from ..paper.state import (
    PaperPosition,
    PaperState,
    load_paper_state,
    save_paper_state,
)
from .broker import (
    AccountInfo,
    Broker,
    BrokerError,
    ClosedTrade,
    OpenPosition,
    OrderRequest,
    OrderResult,
    OrderSide,
)


def _to_order_side(side: str) -> OrderSide:
    return OrderSide.BUY if Side(side) is Side.LONG else OrderSide.SELL


def _to_paper_side(side: OrderSide) -> str:
    return Side.LONG.value if side is OrderSide.BUY else Side.SHORT.value


class PaperBroker:
    """In-process paper broker backed by ``PaperState``."""

    name: str = "paper"

    def __init__(
        self,
        state_path: str | Path,
        *,
        starting_equity: float = 10_000.0,
        symbol: str = "XAUUSD",
    ) -> None:
        self._state_path = Path(state_path)
        self._starting_equity = starting_equity
        self._symbol = symbol

    # ------------------------------------------------------------------
    def _load(self) -> PaperState:
        return load_paper_state(self._state_path, starting_equity=self._starting_equity)

    def _save(self, state: PaperState) -> None:
        save_paper_state(state, self._state_path)

    # ------------------------------------------------------------------
    def get_account_info(self) -> AccountInfo:
        state = self._load()
        return AccountInfo(
            equity=state.paper_equity,
            balance=state.paper_equity,
            currency="USD",
            margin_used=0.0,
            margin_free=state.paper_equity,
            leverage=1.0,
        )

    def get_open_position(self, magic: int = 20260507) -> OpenPosition | None:
        state = self._load()
        pos = state.open_position
        if pos is None:
            return None
        opened_at = datetime.fromisoformat(pos.opened_at)
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        # Paper does not compute unrealised PnL between cycles — the next
        # bar-monitor step does.  Report 0 here so the field is well-defined.
        return OpenPosition(
            broker_order_id=f"paper:{pos.opened_at}",
            symbol=self._symbol,
            side=_to_order_side(pos.side),
            units=0.0,
            entry_price=pos.entry,
            stop_price=pos.stop,
            target_price=pos.target,
            opened_at=opened_at,
            unrealised_pnl=0.0,
            magic=magic,
        )

    def place_market_order(self, request: OrderRequest) -> OrderResult:
        state = self._load()
        if state.open_position is not None:
            return OrderResult(
                accepted=False,
                broker_order_id=None,
                fill_price=None,
                units=None,
                error="paper: position already open",
            )

        # The paper layer keeps risk implicit (1% of equity).  The Broker
        # interface speaks in dollars, so units = risk / |entry - stop|.
        # We need the entry; for paper that's the supplied stop's reference,
        # which the caller is expected to attach via the agent-cycle.  Since
        # the existing paper flow records entry from the decision plan, this
        # method is a thin shim: the caller must pre-populate PaperState's
        # open_position via ``open_position_from_decision``.  This method
        # therefore raises if used directly — paper broker is read-only for
        # placement in the current architecture.
        raise BrokerError(
            "PaperBroker.place_market_order is not the entry point — paper "
            "trades are opened via paper.state.open_position_from_decision."
        )

    def close_position(
        self,
        broker_order_id: str,
        reason: str = "manual",
    ) -> ClosedTrade | None:
        state = self._load()
        pos = state.open_position
        if pos is None:
            return None
        # Manual close: clear the open_position; the closed_positions list is
        # appended through monitor_open_position in normal flow.  Manual close
        # is rare; we keep it simple.
        opened_at = datetime.fromisoformat(pos.opened_at)
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        closed_at = datetime.now(timezone.utc)
        closed = PaperPosition(
            opened_at=pos.opened_at,
            family=pos.family,
            timeframe_minutes=pos.timeframe_minutes,
            side=pos.side,
            entry=pos.entry,
            stop=pos.stop,
            target=pos.target,
            status=f"closed_{reason}",
            closed_at=closed_at.isoformat(),
            closed_price=pos.entry,  # no current bar info here; flat-flat close
            pnl_r=0.0,
            exit_reason=reason,
        )
        new_state = PaperState(
            open_position=None,
            closed_positions=state.closed_positions + [closed],
            paper_equity=state.paper_equity,
            daily_peak_equity=state.daily_peak_equity,
            last_updated=closed_at.isoformat(),
            total_trades=state.total_trades + 1,
            winning_trades=state.winning_trades,
            daily_reset_date=state.daily_reset_date,
            daily_trades_opened=state.daily_trades_opened,
        )
        self._save(new_state)
        return ClosedTrade(
            broker_order_id=broker_order_id,
            symbol=self._symbol,
            side=_to_order_side(pos.side),
            units=0.0,
            entry_price=pos.entry,
            exit_price=pos.entry,
            opened_at=opened_at,
            closed_at=closed_at,
            pnl_dollars=0.0,
            exit_reason=reason,
        )

    def get_pending_order(self, magic: int = 20260507):  # noqa: ARG002
        # Paper broker has no resting orders — paper "entries" fire instantly
        # on signal trigger.  Return None to satisfy the Protocol.
        return None

    def cancel_pending_order(self, broker_order_id: str) -> bool:  # noqa: ARG002
        return False

    def get_deals_since(
        self,
        since,  # noqa: ANN001 - datetime
        magic: int = 20260507,  # noqa: ARG002
    ) -> list:
        # Paper broker has no broker-side deal history; closed-trade truth
        # lives in PaperState.closed_positions already.
        return []


def get_broker_from_env() -> Broker:
    """Resolve the broker selected by the ``GOLD_BROKER`` env var.

    Defaults to ``paper`` against ``data/agent_live_xauusd/paper_state.json``.
    """
    import os

    kind = os.environ.get("GOLD_BROKER", "paper").strip().lower()
    if kind == "paper":
        return PaperBroker(
            state_path=os.environ.get(
                "GOLD_PAPER_STATE",
                "data/agent_live_xauusd/paper_state.json",
            ),
            starting_equity=float(os.environ.get("GOLD_PAPER_EQUITY", "10000")),
            symbol=os.environ.get("GOLD_SYMBOL", "XAUUSD"),
        )
    if kind == "mt5_local":
        from .mt5_broker import MT5LocalBroker

        login_str = os.environ.get("MT5_LOGIN")
        return MT5LocalBroker(
            symbol=os.environ.get("GOLD_SYMBOL", "GOLD"),
            magic=int(os.environ.get("GOLD_MAGIC", "20260507")),
            deviation_points=int(os.environ.get("MT5_DEVIATION", "20")),
            account_type=os.environ.get("MT5_ACCOUNT_TYPE", "demo"),
            login=int(login_str) if login_str else None,
            password=os.environ.get("MT5_PASSWORD"),
            server=os.environ.get("MT5_SERVER"),
            terminal_path=os.environ.get("MT5_TERMINAL_PATH"),
        )
    if kind == "mt5_remote":
        from .mt5_bridge_client import MT5RemoteBroker

        return MT5RemoteBroker(
            base_url=os.environ.get("GOLD_BRIDGE_URL", "http://127.0.0.1:8765"),
            shared_secret=os.environ.get("GOLD_BRIDGE_SECRET", ""),
            timeout=float(os.environ.get("GOLD_BRIDGE_TIMEOUT", "15")),
            magic=int(os.environ.get("GOLD_MAGIC", "20260507")),
        )
    raise BrokerError(f"Unknown GOLD_BROKER='{kind}'.  Valid: paper, mt5_local, mt5_remote")
