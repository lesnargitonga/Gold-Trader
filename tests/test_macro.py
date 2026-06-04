"""Tests for src/gold_trader/data/macro.py.

All network access is patched; tests are fully offline and deterministic.
"""
from __future__ import annotations

import io
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from gold_trader.data.macro import (
    MACRO_BUNDLE,
    MacroFrame,
    MacroPoint,
    MacroSeries,
    fetch_fred_series,
    fetch_stooq_series,
    load_macro_frame,
    load_or_fetch_macro,
    read_macro_csv,
    sync_macro_bundle,
    write_macro_csv,
)


# ---------- MacroSeries primitives -----------------------------------------


def _series(name: str = "us10y") -> MacroSeries:
    pts = [
        MacroPoint(timestamp=datetime(2025, 1, d, tzinfo=timezone.utc), value=4.0 + d * 0.01)
        for d in range(1, 11)
    ]
    return MacroSeries(name=name, source="fred", points=pts)


def test_as_of_returns_last_known_value() -> None:
    s = _series()
    # Exact match
    assert s.as_of(datetime(2025, 1, 5, tzinfo=timezone.utc)) == pytest.approx(4.05)
    # Between points -> previous one
    assert s.as_of(datetime(2025, 1, 5, 12, tzinfo=timezone.utc)) == pytest.approx(4.05)
    # Before first
    assert s.as_of(datetime(2024, 12, 31, tzinfo=timezone.utc)) is None
    # After last -> last value
    assert s.as_of(datetime(2025, 12, 31, tzinfo=timezone.utc)) == pytest.approx(4.10)


def test_change_uses_lookback() -> None:
    s = _series()
    # 5-day change at 2025-01-10 -> value(10) - value(5)
    delta = s.change(datetime(2025, 1, 10, tzinfo=timezone.utc), lookback_days=5)
    assert delta == pytest.approx(0.05, abs=1e-9)


def test_change_returns_none_when_history_too_short() -> None:
    s = _series()
    assert s.change(datetime(2025, 1, 1, tzinfo=timezone.utc), lookback_days=5) is None


def test_change_invalid_lookback_raises() -> None:
    s = _series()
    with pytest.raises(ValueError):
        s.change(datetime(2025, 1, 5, tzinfo=timezone.utc), lookback_days=0)


def test_pct_change_basic() -> None:
    s = _series()
    pct = s.pct_change(datetime(2025, 1, 10, tzinfo=timezone.utc), lookback_days=5)
    # (4.10 - 4.05) / 4.05
    assert pct == pytest.approx(0.05 / 4.05, rel=1e-6)


def test_unsorted_input_is_sorted() -> None:
    pts = [
        MacroPoint(timestamp=datetime(2025, 1, 3, tzinfo=timezone.utc), value=3.0),
        MacroPoint(timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc), value=1.0),
        MacroPoint(timestamp=datetime(2025, 1, 2, tzinfo=timezone.utc), value=2.0),
    ]
    s = MacroSeries(name="t", source="fred", points=pts)
    assert [p.value for p in s.points] == [1.0, 2.0, 3.0]


# ---------- HTTP parsing ----------------------------------------------------


_FRED_BODY = (
    b"DATE,DGS10\n"
    b"2025-01-01,.\n"
    b"2025-01-02,4.50\n"
    b"2025-01-03,4.55\n"
    b"2025-01-06,4.40\n"
)


_STOOQ_BODY = (
    b"Date,Open,High,Low,Close,Volume\n"
    b"2025-01-02,103.5,103.9,103.4,103.7,0\n"
    b"2025-01-03,103.7,104.1,103.5,104.0,0\n"
    b"2025-01-06,104.0,104.2,103.8,103.9,0\n"
)


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):  # noqa: D401
        return self

    def __exit__(self, *a):  # noqa: D401
        return False

    def read(self) -> bytes:
        return self._body


def test_fetch_fred_series_skips_missing() -> None:
    with patch("gold_trader.data.macro.urllib.request.urlopen", return_value=_FakeResponse(_FRED_BODY)):
        s = fetch_fred_series("DGS10", date(2025, 1, 1), date(2025, 1, 6))
    assert len(s.points) == 3
    assert s.points[0].value == pytest.approx(4.50)
    assert s.points[-1].timestamp == datetime(2025, 1, 6, tzinfo=timezone.utc)


def test_fetch_stooq_series_parses_close() -> None:
    with patch("gold_trader.data.macro.urllib.request.urlopen", return_value=_FakeResponse(_STOOQ_BODY)):
        s = fetch_stooq_series("^dxy", date(2025, 1, 2), date(2025, 1, 6))
    assert len(s.points) == 3
    assert s.points[0].value == pytest.approx(103.7)
    assert s.points[-1].value == pytest.approx(103.9)


def test_fetch_stooq_detects_no_data() -> None:
    body = b"No data\n"
    with patch("gold_trader.data.macro.urllib.request.urlopen", return_value=_FakeResponse(body)):
        with pytest.raises(RuntimeError, match="rate-limited or unknown symbol"):
            fetch_stooq_series("^bogus", date(2025, 1, 1), date(2025, 1, 6))


def test_fetch_fred_raises_on_empty_body() -> None:
    with patch("gold_trader.data.macro.urllib.request.urlopen", return_value=_FakeResponse(b"DATE,DGS10\n")):
        with pytest.raises(RuntimeError, match="no usable rows"):
            fetch_fred_series("DGS10", date(2025, 1, 1), date(2025, 1, 6))


# ---------- Cache I/O -------------------------------------------------------


def test_round_trip_csv(tmp_path: Path) -> None:
    s = _series("us10y")
    path = tmp_path / "us10y.csv"
    write_macro_csv(s, path)
    s2 = read_macro_csv(path, name="us10y", source="fred")
    assert len(s2.points) == len(s.points)
    for p1, p2 in zip(s.points, s2.points):
        assert p1.timestamp == p2.timestamp
        assert p1.value == pytest.approx(p2.value)


def test_read_csv_rejects_bad_header(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("ts,v\n2025-01-01,1.0\n")
    with pytest.raises(ValueError, match="Bad macro CSV header"):
        read_macro_csv(path, name="x", source="fred")


# ---------- High-level API --------------------------------------------------


def test_load_or_fetch_uses_cache(tmp_path: Path) -> None:
    s = _series("vix")
    write_macro_csv(s, tmp_path / "vix.csv")
    # Should NOT call HTTP.
    with patch("gold_trader.data.macro.urllib.request.urlopen", side_effect=AssertionError("HTTP not allowed")):
        loaded = load_or_fetch_macro("vix", start=date(2025, 1, 1), end=date(2025, 1, 31), cache_dir=tmp_path, refresh=False)
    assert len(loaded.points) == 10


def test_load_or_fetch_refresh_writes_canonical_name(tmp_path: Path) -> None:
    with patch("gold_trader.data.macro.urllib.request.urlopen", return_value=_FakeResponse(_FRED_BODY)):
        loaded = load_or_fetch_macro("us10y", start=date(2025, 1, 1), end=date(2025, 1, 6), cache_dir=tmp_path, refresh=True)
    assert loaded.name == "us10y"  # not "DGS10"
    assert (tmp_path / "us10y.csv").exists()


def test_unknown_name_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="Unknown macro series"):
        load_or_fetch_macro("not_a_thing", start=date(2025, 1, 1), end=date(2025, 1, 6), cache_dir=tmp_path)


def test_sync_macro_bundle_collects_status(tmp_path: Path) -> None:
    # All FRED now; mock all calls to succeed.
    with patch("gold_trader.data.macro.urllib.request.urlopen", return_value=_FakeResponse(_FRED_BODY)):
        status = sync_macro_bundle(tmp_path, start=date(2025, 1, 1), end=date(2025, 1, 6), refresh=True)

    for name in MACRO_BUNDLE:
        assert status[name] == "ok"


def test_load_macro_frame_skips_missing(tmp_path: Path) -> None:
    write_macro_csv(_series("us10y"), tmp_path / "us10y.csv")
    write_macro_csv(_series("vix"), tmp_path / "vix.csv")
    frame = load_macro_frame(tmp_path)
    assert "us10y" in frame
    assert "vix" in frame
    # dxy not on disk -> skipped, no error.
    assert frame.get("dxy") is None
    with pytest.raises(KeyError):
        frame.require("dxy")


def test_macro_bundle_contents_are_stable() -> None:
    """Lock the canonical bundle so accidental rewording is caught in CI."""
    assert set(MACRO_BUNDLE) == {"us10y", "us2y", "real10y", "vix", "dxy", "spx", "usdjpy"}
    assert MACRO_BUNDLE["us10y"] == ("fred", "DGS10")
    assert MACRO_BUNDLE["dxy"] == ("fred", "DTWEXBGS")
