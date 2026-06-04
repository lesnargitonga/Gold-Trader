"""Paper-trade journal: append-only log of every closed paper trade
enriched with regime tags + macro filter verdict + execution drift.

Designed to be invoked at the *end* of every ``agent-cycle`` run.  Reads
the paper_state.json, finds rows whose ``closed_at`` is newer than the
last row already in the journal, computes regime + filter context at
the trade's *entry* time, and appends to ``logs/trade_journal.csv``.

This is the ground-truth record we'll use after 30+ trades to answer:
*does MacroDecisionFilter have empirical lift on forward data?*
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .data import load_bars_from_csv
from .data.macro import MacroFrame, load_macro_frame
from .macro_filter import MacroDecisionFilter
from .models import MarketBar, Side
from .regime import RegimeDetector


JOURNAL_HEADER = [
    "closed_at",
    "opened_at",
    "family",
    "tf",
    "side",
    "entry",
    "stop",
    "target",
    "exit_price",
    "exit_reason",
    "expected_r",
    "realised_r",
    "drift_r",
    "filter_verdict",
    "filter_reason",
    "regime_vol_pct",
    "regime_trend",
    "regime_compression",
    "regime_spread",
    "regime_macro_real10y",
    "regime_macro_dxy",
    "regime_macro_vix",
    "regime_macro_stagflation",
    "regime_session_vwap",
]


@dataclass
class JournalRow:
    data: dict[str, object]

    def to_csv_row(self) -> list[str]:
        return [str(self.data.get(k, "")) for k in JOURNAL_HEADER]


def _r_value(side: str, entry: float, stop: float, price: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    direction = (
        1.0
        if side.lower().endswith("long") or side.lower() == "buy"
        else -1.0
    )
    return (price - entry) * direction / risk


def _side_enum(side_str: str) -> Side:
    return Side.LONG if side_str.lower().endswith("long") or side_str.lower() == "buy" else Side.SHORT


def _existing_closed_ats(journal_path: Path) -> set[str]:
    if not journal_path.exists():
        return set()
    seen: set[str] = set()
    with journal_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ca = row.get("closed_at")
            if ca:
                seen.add(ca)
    return seen


def _parse_entry_ts(opened_at: str) -> datetime | None:
    try:
        return datetime.fromisoformat(opened_at)
    except (ValueError, TypeError):
        return None


def _find_bar_index(bars: list[MarketBar], ts: datetime) -> int | None:
    """Last bar index with bar.timestamp <= ts."""
    if not bars:
        return None
    if ts.tzinfo is None and bars[0].timestamp.tzinfo is not None:
        ts = ts.replace(tzinfo=bars[0].timestamp.tzinfo)
    lo, hi = 0, len(bars) - 1
    if ts < bars[0].timestamp:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if bars[mid].timestamp <= ts:
            lo = mid
        else:
            hi = mid - 1
    return lo


def update_journal(
    paper_state_path: Path,
    journal_path: Path,
    bars_csv: Path | None = None,
    macro_dir: Path | None = None,
) -> int:
    """Read paper_state, append new closed trades to the journal.

    Returns the number of rows appended.
    """
    if not paper_state_path.exists():
        return 0
    with paper_state_path.open("r") as f:
        state = json.load(f)
    closed = state.get("closed_positions", [])
    if not closed:
        return 0

    seen = _existing_closed_ats(journal_path)
    new_rows = [p for p in closed if str(p.get("closed_at", "")) not in seen]
    if not new_rows:
        return 0

    bars: list[MarketBar] = []
    if bars_csv is not None and bars_csv.exists():
        try:
            bars = load_bars_from_csv(str(bars_csv))
        except Exception:
            bars = []

    macro: MacroFrame | None = None
    if macro_dir is not None:
        try:
            mf = load_macro_frame(macro_dir)
            if len(mf.names()) > 0:
                macro = mf
        except Exception:
            macro = None

    detector = RegimeDetector()
    fltr = MacroDecisionFilter(macro=macro) if macro is not None else None

    journal_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not journal_path.exists()
    with journal_path.open("a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(JOURNAL_HEADER)
        for pos in new_rows:
            row = _build_row(pos, bars, detector, fltr)
            writer.writerow(JournalRow(row).to_csv_row())

    return len(new_rows)


def _build_row(
    pos: dict,
    bars: list[MarketBar],
    detector: RegimeDetector,
    fltr: MacroDecisionFilter | None,
) -> dict[str, object]:
    side_str = str(pos.get("side", "long"))
    entry = float(pos.get("entry", 0.0))
    stop = float(pos.get("stop", 0.0))
    target = float(pos.get("target", 0.0))
    exit_price = pos.get("closed_price")
    exit_reason = pos.get("exit_reason") or "unknown"
    expected_r = 0.0
    realised_r = 0.0
    drift = 0.0
    if exit_price is not None:
        exit_price_f = float(exit_price)
        realised_r = _r_value(side_str, entry, stop, exit_price_f)
        if exit_reason == "target":
            expected_r = _r_value(side_str, entry, stop, target)
        elif exit_reason == "stop":
            expected_r = -1.0
        else:
            expected_r = realised_r
        drift = realised_r - expected_r

    # Regime + filter context at *entry* time.
    opened_at = str(pos.get("opened_at") or "")
    entry_ts = _parse_entry_ts(opened_at)
    regime: dict[str, object] = {}
    verdict_str = "n/a"
    verdict_reason = ""
    if entry_ts is not None and bars:
        idx = _find_bar_index(bars, entry_ts)
        if idx is not None and idx >= 1:
            try:
                tags = detector.classify(
                    bars, idx,
                    macro=fltr.macro if fltr is not None else None,
                )
                regime = tags.to_dict()
            except Exception:
                regime = {}
    if fltr is not None and entry_ts is not None:
        try:
            v = fltr.evaluate(_side_enum(side_str), entry_ts)
            verdict_str = v.verdict
            verdict_reason = v.reason
        except Exception:
            pass

    return {
        "closed_at": pos.get("closed_at") or "",
        "opened_at": opened_at,
        "family": pos.get("family") or "",
        "tf": pos.get("timeframe_minutes") or "",
        "side": side_str,
        "entry": entry,
        "stop": stop,
        "target": target,
        "exit_price": exit_price if exit_price is not None else "",
        "exit_reason": exit_reason,
        "expected_r": round(expected_r, 4),
        "realised_r": round(realised_r, 4),
        "drift_r": round(drift, 4),
        "filter_verdict": verdict_str,
        "filter_reason": verdict_reason,
        "regime_vol_pct": regime.get("vol_pct", ""),
        "regime_trend": regime.get("trend", ""),
        "regime_compression": regime.get("compression", ""),
        "regime_spread": regime.get("spread", ""),
        "regime_macro_real10y": regime.get("macro_real10y", ""),
        "regime_macro_dxy": regime.get("macro_dxy", ""),
        "regime_macro_vix": regime.get("macro_vix", ""),
        "regime_macro_stagflation": regime.get("macro_stagflation", ""),
        "regime_session_vwap": regime.get("session_vwap", ""),
    }


def read_journal(journal_path: Path) -> list[dict[str, str]]:
    if not journal_path.exists():
        return []
    with journal_path.open("r", newline="") as f:
        return list(csv.DictReader(f))
