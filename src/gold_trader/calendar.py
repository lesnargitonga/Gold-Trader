"""Economic-calendar news blackout.

Loads a CSV of high-impact USD events (NFP, CPI, FOMC, PPI, retail sales,
etc.) and exposes a fast ``is_blackout(ts, window_minutes)`` check.

CSV format (header required), columns::

    timestamp,event,impact

* ``timestamp`` ISO-8601 UTC (e.g. ``2026-05-09T12:30:00Z``)
* ``event`` free-form short name (e.g. ``NFP``, ``CPI YoY``)
* ``impact`` one of ``low|medium|high`` (only ``high`` triggers blackout
  by default; configurable via ``min_impact`` argument).

Default file: ``data/macro/news_calendar.csv`` (created empty if not
present).  Operators can append rows manually or via the web UI later.

Usage::

    cal = NewsCalendar.load(Path("data/macro/news_calendar.csv"))
    if cal.is_blackout(now, window_minutes=15):
        skip_trading()
"""
from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

DEFAULT_CALENDAR_PATH = Path("data/macro/news_calendar.csv")
_HEADER = ["timestamp", "event", "impact"]
_IMPACT_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class NewsEvent:
    timestamp: datetime
    event: str
    impact: str  # low | medium | high


@dataclass
class NewsCalendar:
    """Sorted list of news events with blackout checks."""

    events: list[NewsEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.events = sorted(self.events, key=lambda e: e.timestamp)
        self._ts_index = [e.timestamp for e in self.events]

    @classmethod
    def load(cls, path: Path | None = None) -> "NewsCalendar":
        p = path or DEFAULT_CALENDAR_PATH
        if not p.exists():
            return cls(events=[])
        events: list[NewsEvent] = []
        try:
            with p.open("r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts_raw = (row.get("timestamp") or "").strip()
                    if not ts_raw:
                        continue
                    try:
                        ts = _parse_iso(ts_raw)
                    except ValueError:
                        continue
                    impact = (row.get("impact") or "high").strip().lower()
                    if impact not in _IMPACT_RANK:
                        impact = "high"
                    events.append(NewsEvent(
                        timestamp=ts,
                        event=(row.get("event") or "").strip()[:80],
                        impact=impact,
                    ))
        except Exception:
            return cls(events=[])
        return cls(events=events)

    def save(self, path: Path | None = None) -> None:
        p = path or DEFAULT_CALENDAR_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(_HEADER)
            for e in self.events:
                w.writerow([_to_iso(e.timestamp), e.event, e.impact])

    def add(self, event: NewsEvent) -> None:
        bisect.insort(self.events, event, key=lambda x: x.timestamp)
        self._ts_index = [e.timestamp for e in self.events]

    def upcoming(self, now: datetime, max_count: int = 10) -> list[NewsEvent]:
        if not self.events:
            return []
        now = _normalize(now)
        i = bisect.bisect_left(self._ts_index, now)
        return self.events[i : i + max_count]

    def nearest(self, ts: datetime) -> NewsEvent | None:
        """Return the calendar event closest in time to ``ts`` (any direction)."""
        if not self.events:
            return None
        ts = _normalize(ts)
        i = bisect.bisect_left(self._ts_index, ts)
        cands: list[NewsEvent] = []
        if i < len(self.events):
            cands.append(self.events[i])
        if i > 0:
            cands.append(self.events[i - 1])
        return min(cands, key=lambda e: abs((e.timestamp - ts).total_seconds()))

    def is_blackout(
        self,
        ts: datetime,
        window_minutes: float = 15.0,
        min_impact: str = "high",
    ) -> tuple[bool, NewsEvent | None]:
        """True if ``ts`` is within ``window_minutes`` of a qualifying event.

        Returns ``(blocked, triggering_event_or_None)``.
        """
        if not self.events or window_minutes <= 0:
            return False, None
        ts = _normalize(ts)
        threshold = _IMPACT_RANK.get(min_impact.lower(), 2)
        delta = timedelta(minutes=float(window_minutes))
        lo = ts - delta
        hi = ts + delta
        i = bisect.bisect_left(self._ts_index, lo)
        while i < len(self.events) and self.events[i].timestamp <= hi:
            ev = self.events[i]
            if _IMPACT_RANK.get(ev.impact, 0) >= threshold:
                return True, ev
            i += 1
        return False, None


# ---------------------------------------------------------------- helpers


def _parse_iso(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_iso(dt: datetime) -> str:
    return _normalize(dt).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _normalize(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
