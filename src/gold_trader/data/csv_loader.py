from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path

from ..models import MarketBar


def read_last_bar_timestamp(path: str | Path) -> datetime | None:
    """Return the timestamp of the last bar in *path* without loading the whole file.

    Uses a tail-seek so this is O(1) regardless of file size.  Returns None if
    the file does not exist or contains only a header row.
    """
    csv_path = Path(path)
    if not csv_path.exists():
        return None

    file_size = csv_path.stat().st_size
    if file_size == 0:
        return None

    # Read up to 512 bytes from the end — more than enough for one CSV row.
    chunk_size = min(512, file_size)
    with csv_path.open("rb") as fh:
        fh.seek(-chunk_size, os.SEEK_END)
        tail = fh.read().decode("utf-8", errors="replace")

    # The last non-empty line is the last data row.
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if not lines:
        return None

    last_line = lines[-1]
    # Guard: if the last line looks like a header, there's no data yet.
    if last_line.startswith("timestamp"):
        return None

    timestamp_field = last_line.split(",")[0].strip()
    try:
        return _parse_timestamp(timestamp_field)
    except (ValueError, IndexError):
        return None


def load_bars_from_csv(path: str | Path) -> list[MarketBar]:
    csv_path = Path(path)
    bars: list[MarketBar] = []

    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            timestamp = _parse_timestamp(row["timestamp"])
            bars.append(
                MarketBar(
                    timestamp=timestamp,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0.0),
                    spread=float(row.get("spread") or 0.0),
                    session=(row.get("session") or "unknown").strip().lower(),
                    news_distance_minutes=_optional_float(row.get("news_distance_minutes")),
                    dxy_close=_optional_float(row.get("dxy_close")),
                )
            )

    return bars


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return float(stripped)