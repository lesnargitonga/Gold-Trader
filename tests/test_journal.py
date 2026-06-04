"""Tests for the trade journal append-only writer."""
from __future__ import annotations

import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from gold_trader.journal import (
    JOURNAL_HEADER,
    update_journal,
    read_journal,
)


def _write_state(path: Path, closed_trades: list[dict]) -> None:
    state = {
        "open_position": None,
        "closed_positions": closed_trades,
        "paper_equity": 10_000.0,
        "daily_peak_equity": 10_000.0,
        "last_updated": "2026-05-08T00:00:00+00:00",
        "total_trades": len(closed_trades),
        "winning_trades": 0,
    }
    path.write_text(json.dumps(state))


def _trade(closed_at: str, exit_reason: str = "stop", closed_price: float = 1990.0) -> dict:
    return {
        "opened_at": "2026-05-07T15:00:00+00:00",
        "family": "asian_range_breakout",
        "timeframe_minutes": 60,
        "side": "Side.LONG",
        "entry": 2000.0,
        "stop": 1990.0,
        "target": 2025.0,
        "status": "closed",
        "closed_at": closed_at,
        "closed_price": closed_price,
        "pnl_r": -1.0,
        "exit_reason": exit_reason,
    }


class TradeJournalTests(unittest.TestCase):
    def test_writes_header_and_rows(self) -> None:
        with TemporaryDirectory() as td:
            tdp = Path(td)
            state_path = tdp / "paper_state.json"
            journal_path = tdp / "journal.csv"
            _write_state(state_path, [_trade("2026-05-07T18:00:00+00:00")])
            n = update_journal(state_path, journal_path)
            self.assertEqual(n, 1)
            self.assertTrue(journal_path.exists())
            with journal_path.open() as f:
                header = next(csv.reader(f))
            self.assertEqual(header, JOURNAL_HEADER)

    def test_idempotent(self) -> None:
        with TemporaryDirectory() as td:
            tdp = Path(td)
            state_path = tdp / "paper_state.json"
            journal_path = tdp / "journal.csv"
            _write_state(state_path, [_trade("2026-05-07T18:00:00+00:00")])
            self.assertEqual(update_journal(state_path, journal_path), 1)
            # Second call: same closed trade, no duplicate.
            self.assertEqual(update_journal(state_path, journal_path), 0)
            rows = read_journal(journal_path)
            self.assertEqual(len(rows), 1)

    def test_appends_only_new(self) -> None:
        with TemporaryDirectory() as td:
            tdp = Path(td)
            state_path = tdp / "paper_state.json"
            journal_path = tdp / "journal.csv"
            _write_state(state_path, [_trade("2026-05-07T18:00:00+00:00")])
            update_journal(state_path, journal_path)
            # Add a second closed trade.
            _write_state(state_path, [
                _trade("2026-05-07T18:00:00+00:00"),
                _trade("2026-05-07T19:00:00+00:00", exit_reason="target", closed_price=2025.0),
            ])
            n = update_journal(state_path, journal_path)
            self.assertEqual(n, 1)
            rows = read_journal(journal_path)
            self.assertEqual(len(rows), 2)
            # Realised R for the target trade should be +2.5 (target hit).
            target_row = [r for r in rows if r["exit_reason"] == "target"][0]
            self.assertAlmostEqual(float(target_row["realised_r"]), 2.5, places=2)

    def test_empty_state_is_safe(self) -> None:
        with TemporaryDirectory() as td:
            tdp = Path(td)
            state_path = tdp / "paper_state.json"
            journal_path = tdp / "journal.csv"
            _write_state(state_path, [])
            self.assertEqual(update_journal(state_path, journal_path), 0)


if __name__ == "__main__":
    unittest.main()
