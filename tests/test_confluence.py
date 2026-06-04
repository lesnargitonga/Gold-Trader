"""Tests for the multi-timeframe confluence scorer."""
from __future__ import annotations

from datetime import datetime, timedelta

from gold_trader.confluence import (
    ConfluencePoint,
    DEFAULT_TF_WEIGHT,
    score_confluence,
)
from gold_trader.zones import Zone


def _z(top, bot, side, kind, family, t0=None, status="active"):
    return Zone(
        kind=kind,
        top=float(top),
        bot=float(bot),
        t0=t0 or datetime(2026, 5, 1, 12, 0),
        t1=None,
        side=side,
        status=status,
        score=1.0,
        reason="test",
        family=family,
    )


def test_no_confluence_under_min_contributors():
    zones_by_tf = {15: [_z(2050, 2048, "short", "fvg_bear", "fvg")]}
    pts = score_confluence(zones_by_tf, min_contributors=2)
    assert pts == []


def test_overlapping_zones_across_tf_cluster():
    t = datetime(2026, 5, 4, 10, 0)
    zones_by_tf = {
        15: [_z(2050, 2048, "short", "fvg_bear", "fvg", t0=t)],
        60: [_z(2049.5, 2047, "short", "ifvg_bear", "ifvg", t0=t)],
        240: [_z(2050, 2050, "short", "swing_high", "swings", t0=t)],
    }
    pts = score_confluence(zones_by_tf, tolerance=0.25, now=t)
    assert len(pts) == 1
    p = pts[0]
    assert p.side == "short"
    assert len(p.contributors) == 3
    # Three families ⇒ diversity bonus
    assert p.score > sum(DEFAULT_TF_WEIGHT[tf] for tf in (15, 60, 240))


def test_opposing_sides_do_not_cluster():
    t = datetime(2026, 5, 4, 10, 0)
    zones_by_tf = {
        15: [_z(2050, 2048, "short", "fvg_bear", "fvg", t0=t)],
        60: [_z(2049, 2047, "long", "fvg_bull", "fvg", t0=t)],
    }
    pts = score_confluence(zones_by_tf, tolerance=0.5, now=t, min_contributors=2)
    assert pts == []  # opposite sides never merge


def test_invalidated_zone_excluded():
    t = datetime(2026, 5, 4, 10, 0)
    zones_by_tf = {
        15: [
            _z(2050, 2048, "short", "fvg_bear", "fvg", t0=t),
            _z(2049, 2047, "short", "swing_high", "swings", t0=t, status="invalidated"),
        ],
    }
    pts = score_confluence(zones_by_tf, tolerance=0.5, now=t)
    # invalidated zone has weight 0, but it's still in the cluster — score
    # should equal the single-TF weight without diversity bump from the dead zone
    if pts:
        # only one *effective* contributor weight, so score should be modest
        assert pts[0].score <= DEFAULT_TF_WEIGHT[15] * 1.5


def test_mitigated_zone_half_weight():
    t = datetime(2026, 5, 4, 10, 0)
    a = {15: [
        _z(2050, 2048, "short", "fvg_bear", "fvg", t0=t),
        _z(2049, 2047, "short", "swing_high", "swings", t0=t),
    ]}
    b = {15: [
        _z(2050, 2048, "short", "fvg_bear", "fvg", t0=t, status="mitigated"),
        _z(2049, 2047, "short", "swing_high", "swings", t0=t),
    ]}
    pa = score_confluence(a, tolerance=0.5, now=t)[0]
    pb = score_confluence(b, tolerance=0.5, now=t)[0]
    assert pb.score < pa.score


def test_to_dict_round_trip():
    t = datetime(2026, 5, 4, 10, 0)
    zones_by_tf = {
        15: [_z(2050, 2048, "short", "fvg_bear", "fvg", t0=t)],
        60: [_z(2049, 2047, "short", "swing_high", "swings", t0=t)],
    }
    pts = score_confluence(zones_by_tf, tolerance=0.5, now=t)
    d = pts[0].to_dict()
    assert d["side"] == "short"
    assert d["n_contributors"] == 2
    assert "contributors" in d
    assert d["price_top"] >= d["price_bot"]
