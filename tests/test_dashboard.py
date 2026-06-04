"""Tests for the static HTML dashboard renderer."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from gold_trader.live.broker import AccountInfo, OpenPosition, OrderSide
from gold_trader.paper.state import PaperPosition, PaperState
from gold_trader.reports.dashboard import (
    render_dashboard,
    write_dashboard,
)


def _state_flat() -> PaperState:
    return PaperState(
        open_position=None,
        closed_positions=[],
        paper_equity=10000.0,
        daily_peak_equity=10000.0,
        last_updated="2026-05-07T12:00:00+00:00",
        total_trades=0,
        winning_trades=0,
    )


def _state_with_history() -> PaperState:
    closed = [
        PaperPosition(
            opened_at="2026-05-01T10:00:00+00:00",
            family="asian_range_breakout",
            timeframe_minutes=60,
            side="long",
            entry=4700.0,
            stop=4690.0,
            target=4720.0,
            status="closed_target",
            closed_at="2026-05-01T18:00:00+00:00",
            closed_price=4720.0,
            pnl_r=2.0,
            exit_reason="target",
        ),
        PaperPosition(
            opened_at="2026-05-02T10:00:00+00:00",
            family="asian_range_breakout",
            timeframe_minutes=60,
            side="short",
            entry=4730.0,
            stop=4740.0,
            target=4710.0,
            status="closed_stop",
            closed_at="2026-05-02T18:00:00+00:00",
            closed_price=4740.0,
            pnl_r=-1.0,
            exit_reason="stop",
        ),
    ]
    open_pos = PaperPosition(
        opened_at="2026-05-07T12:00:00+00:00",
        family="asian_range_breakout",
        timeframe_minutes=60,
        side="long",
        entry=4746.36,
        stop=4685.32,
        target=4843.04,
    )
    return PaperState(
        open_position=open_pos,
        closed_positions=closed,
        paper_equity=10100.0,
        daily_peak_equity=10100.0,
        last_updated="2026-05-07T12:30:00+00:00",
        total_trades=2,
        winning_trades=1,
    )


class DashboardTests(unittest.TestCase):
    def test_render_flat_state(self) -> None:
        html = render_dashboard(_state_flat())
        self.assertIn("<!doctype html>", html)
        self.assertIn("$10,000.00", html)
        self.assertIn("No open position", html)
        self.assertIn("No closed trades yet", html)

    def test_render_with_history_includes_trades_and_open(self) -> None:
        html = render_dashboard(
            _state_with_history(),
            broker_name="paper",
            account=AccountInfo(10100.0, 10100.0, "USD", 0.0, 10100.0, 1.0),
        )
        self.assertIn("OPEN POSITION", html)
        self.assertIn("4746.36", html)
        self.assertIn("asian_range_breakout", html)
        self.assertIn("+2.000R", html)
        self.assertIn("-1.000R", html)
        self.assertIn("Closed trades", html)
        # Account card rendered.
        self.assertIn("paper", html)
        self.assertIn("USD", html)

    def test_render_with_broker_error_shows_fallback(self) -> None:
        html = render_dashboard(
            _state_flat(),
            broker_name="mt5_local",
            account=None,
            broker_error="MetaTrader5 not installed",
        )
        self.assertIn("Failed to query", html)
        self.assertIn("MetaTrader5 not installed", html)

    def test_render_with_open_broker_position(self) -> None:
        op = OpenPosition(
            broker_order_id="paper:2026-05-07T12:00:00+00:00",
            symbol="GOLD",
            side=OrderSide.BUY,
            units=0.10,
            entry_price=4746.36,
            stop_price=4685.32,
            target_price=4843.04,
            opened_at=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
            unrealised_pnl=12.50,
        )
        html = render_dashboard(
            _state_with_history(),
            broker_name="paper",
            account=AccountInfo(10100.0, 10100.0, "USD", 0.0, 10100.0, 1.0),
            op_broker=op,
        )
        self.assertIn("paper:2026-05-07T12:00:00+00:00", html)
        self.assertIn("$12.50", html)

    def test_write_dashboard_creates_file(self) -> None:
        with TemporaryDirectory() as td:
            out = Path(td) / "nested" / "index.html"
            path = write_dashboard(_state_flat(), out)
            self.assertTrue(path.exists())
            content = path.read_text(encoding="utf-8")
            self.assertIn("<!doctype html>", content)

    def test_html_escapes_user_text(self) -> None:
        # Ensure family names with HTML chars are escaped (defensive).
        state = _state_with_history()
        bad = state.closed_positions[0]
        # Replace family with HTML-injection attempt.
        evil = PaperPosition(
            opened_at=bad.opened_at,
            family="<script>alert(1)</script>",
            timeframe_minutes=bad.timeframe_minutes,
            side=bad.side,
            entry=bad.entry,
            stop=bad.stop,
            target=bad.target,
            status=bad.status,
            closed_at=bad.closed_at,
            closed_price=bad.closed_price,
            pnl_r=bad.pnl_r,
            exit_reason=bad.exit_reason,
        )
        state2 = PaperState(
            open_position=None,
            closed_positions=[evil],
            paper_equity=state.paper_equity,
            daily_peak_equity=state.daily_peak_equity,
            last_updated=state.last_updated,
            total_trades=state.total_trades,
            winning_trades=state.winning_trades,
        )
        html = render_dashboard(state2)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
