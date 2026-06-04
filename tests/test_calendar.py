"""Tests for NewsCalendar + blackout window."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from gold_trader.calendar import NewsCalendar, NewsEvent


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class CalendarTests(unittest.TestCase):
    def test_load_missing_returns_empty(self) -> None:
        cal = NewsCalendar.load(Path("/tmp/__never_exists__.csv"))
        self.assertEqual(cal.events, [])
        self.assertFalse(cal.is_blackout(datetime.now(timezone.utc), 15)[0])

    def test_save_and_reload(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "cal.csv"
            cal = NewsCalendar(events=[
                NewsEvent(_ts("2026-05-09T12:30:00"), "NFP", "high"),
                NewsEvent(_ts("2026-05-13T12:30:00"), "CPI YoY", "high"),
            ])
            cal.save(p)
            cal2 = NewsCalendar.load(p)
            self.assertEqual(len(cal2.events), 2)
            self.assertEqual(cal2.events[0].event, "NFP")

    def test_blackout_window(self) -> None:
        cal = NewsCalendar(events=[NewsEvent(_ts("2026-05-09T12:30:00"), "NFP", "high")])
        # 5 minutes before -> inside ±15min window
        ok, ev = cal.is_blackout(_ts("2026-05-09T12:25:00"), 15)
        self.assertTrue(ok)
        self.assertIsNotNone(ev)
        # 30 minutes after -> outside window
        ok, ev = cal.is_blackout(_ts("2026-05-09T13:00:00"), 15)
        self.assertFalse(ok)

    def test_min_impact_filter(self) -> None:
        cal = NewsCalendar(events=[NewsEvent(_ts("2026-05-09T12:30:00"), "x", "medium")])
        # Default min_impact='high' -> medium event ignored.
        self.assertFalse(cal.is_blackout(_ts("2026-05-09T12:25:00"), 15)[0])
        # Lower threshold -> matches.
        self.assertTrue(cal.is_blackout(_ts("2026-05-09T12:25:00"), 15, min_impact="medium")[0])

    def test_disabled_when_window_zero(self) -> None:
        cal = NewsCalendar(events=[NewsEvent(_ts("2026-05-09T12:30:00"), "NFP", "high")])
        ok, _ = cal.is_blackout(_ts("2026-05-09T12:30:00"), window_minutes=0)
        self.assertFalse(ok)

    def test_add_keeps_sorted(self) -> None:
        cal = NewsCalendar(events=[NewsEvent(_ts("2026-05-13T12:30:00"), "CPI", "high")])
        cal.add(NewsEvent(_ts("2026-05-09T12:30:00"), "NFP", "high"))
        self.assertEqual([e.event for e in cal.events], ["NFP", "CPI"])


if __name__ == "__main__":
    unittest.main()
