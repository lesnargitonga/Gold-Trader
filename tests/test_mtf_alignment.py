"""Tests for multi-timeframe alignment infrastructure."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gold_trader.data.mtf import (
    MTFBundle,
    TF_MINUTES,
    build_alignment,
    build_mtf_bundle,
    tf_duration,
)
from gold_trader.models import MarketBar


def _bars(start: datetime, count: int, minutes: int) -> list[MarketBar]:
    out: list[MarketBar] = []
    t = start
    for i in range(count):
        out.append(MarketBar(
            timestamp=t,
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
        ))
        t = t + timedelta(minutes=minutes)
    return out


def test_tf_duration_known():
    assert tf_duration("1m") == timedelta(minutes=1)
    assert tf_duration("60m") == timedelta(hours=1)
    assert tf_duration("240m") == timedelta(hours=4)
    assert tf_duration("1440m") == timedelta(days=1)


def test_tf_duration_unknown_raises():
    with pytest.raises(ValueError):
        tf_duration("7m")


def test_build_alignment_no_lookahead():
    """A 60m bar at 12:00 closes at 13:00.  At primary 12:30 the latest
    *closed* 60m bar must be the 11:00 one (closed 12:00), NOT the 12:00
    one which is still forming."""
    epoch = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    primary = _bars(epoch, count=12, minutes=15)  # 10:00 .. 12:45
    htf = _bars(epoch, count=4, minutes=60)        # 10:00, 11:00, 12:00, 13:00

    align = build_alignment(primary, htf, "60m")
    assert len(align) == 12

    # primary[0] = 10:00 → no closed 60m yet → -1
    assert align[0] == -1
    # primary[3] = 10:45 → still warming, 60m at 10:00 closes at 11:00 > 10:45 → -1
    assert align[3] == -1
    # primary[4] = 11:00 → 60m@10:00 just closed (11:00) → idx 0
    assert align[4] == 0
    # primary[7] = 11:45 → 60m@10:00 closed (idx 0) but 11:00 not yet (closes 12:00) → 0
    assert align[7] == 0
    # primary[8] = 12:00 → 60m@11:00 just closed → idx 1
    assert align[8] == 1
    # primary[11] = 12:45 → still idx 1 (12:00 closes 13:00)
    assert align[11] == 1


def test_build_alignment_empty_htf():
    epoch = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    primary = _bars(epoch, count=5, minutes=15)
    align = build_alignment(primary, [], "60m")
    assert align == [-1] * 5


def test_build_alignment_empty_primary():
    epoch = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    htf = _bars(epoch, count=3, minutes=60)
    align = build_alignment([], htf, "60m")
    assert align == []


def test_build_mtf_bundle_query():
    epoch = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    primary = _bars(epoch, count=20, minutes=15)
    htf60 = _bars(epoch, count=6, minutes=60)
    htf240 = _bars(epoch, count=2, minutes=240)

    bundle = build_mtf_bundle(
        "15m",
        primary,
        {"60m": htf60, "240m": htf240},
    )

    assert bundle.primary_tf == "15m"
    assert bundle.htf_codes == ("60m", "240m")
    assert len(bundle.primary_bars) == 20

    # At primary index 0 (10:00) nothing closed yet on either HTF
    assert bundle.htf_index_at("60m", 0) == -1
    assert bundle.htf_bar_at("60m", 0) is None
    assert bundle.htf_slice("60m", 0) == ()

    # primary index 4 = 11:00 → 60m idx 0 is closed
    assert bundle.htf_index_at("60m", 4) == 0
    assert bundle.htf_bar_at("60m", 4) is htf60[0]
    assert bundle.htf_slice("60m", 4) == (htf60[0],)

    # 240m at primary 11:00 still warming (closes at 14:00)
    assert bundle.htf_index_at("240m", 4) == -1


def test_build_mtf_bundle_rejects_lower_or_equal_htf():
    epoch = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    primary = _bars(epoch, count=4, minutes=60)
    htf15 = _bars(epoch, count=4, minutes=15)
    with pytest.raises(ValueError):
        build_mtf_bundle("60m", primary, {"15m": htf15})

    # Equal also rejected
    with pytest.raises(ValueError):
        build_mtf_bundle("60m", primary, {"60m": _bars(epoch, 4, 60)})


def test_build_mtf_bundle_rejects_unknown_tf():
    epoch = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    primary = _bars(epoch, count=4, minutes=15)
    with pytest.raises(ValueError):
        build_mtf_bundle("7m", primary, {})
    with pytest.raises(ValueError):
        build_mtf_bundle("15m", primary, {"7m": _bars(epoch, 1, 7)})


def test_bundle_index_bounds():
    epoch = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    bundle = build_mtf_bundle(
        "15m",
        _bars(epoch, 8, 15),
        {"60m": _bars(epoch, 4, 60)},
    )
    with pytest.raises(IndexError):
        bundle.htf_index_at("60m", -1)
    with pytest.raises(IndexError):
        bundle.htf_index_at("60m", 8)
    with pytest.raises(KeyError):
        bundle.htf_index_at("240m", 0)


def test_alignment_sparse_htf_is_safe():
    """If HTF series has gaps, alignment must still return the latest
    closed bar regardless of cadence."""
    epoch = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)
    primary = _bars(epoch, count=20, minutes=15)
    # Sparse HTF: only two bars at 10:00 and 14:00
    htf = [
        MarketBar(timestamp=epoch, open=1, high=1, low=1, close=1),
        MarketBar(timestamp=epoch + timedelta(hours=4), open=1, high=1, low=1, close=1),
    ]
    align = build_alignment(primary, htf, "60m")
    # 60m@10:00 closes at 11:00 — primary[4]=11:00 is first to see it
    assert align[3] == -1
    assert align[4] == 0
    # 60m@14:00 closes at 15:00 — primary[20] would be 15:00 but we only
    # have 20 bars (10:00 + 19*15min = 14:45) → all idx 4..19 still see only 0
    for i in range(4, 20):
        assert align[i] == 0
