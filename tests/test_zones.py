from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from gold_trader.models import MarketBar
from gold_trader.zones import (
    Zone,
    all_zones,
    find_asian_range,
    find_fvgs,
    find_inversion_fvgs,
    find_prev_day_levels,
    find_swing_pivots,
)


def _bar(t, o, h, l, c, session="unknown"):
    return MarketBar(
        timestamp=t, open=o, high=h, low=l, close=c,
        volume=0.0, spread=0.0, session=session,
    )


class ZoneTests(unittest.TestCase):
    def test_zone_helpers(self) -> None:
        z = Zone(
            kind="fvg_bull", top=2010.0, bot=2000.0,
            t0=datetime(2026, 5, 1, 10, tzinfo=timezone.utc),
        )
        self.assertTrue(z.contains_price(2005.0))
        self.assertFalse(z.contains_price(1999.0))
        self.assertFalse(z.is_level())
        d = z.to_dict()
        self.assertEqual(d["kind"], "fvg_bull")
        self.assertEqual(d["top"], 2010.0)

    def test_find_fvgs_bullish(self) -> None:
        t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
        bars = [
            _bar(t0 + timedelta(minutes=15 * i),
                 o=2000 + i, h=2002 + i, l=1999 + i, c=2001 + i)
            for i in range(2)
        ]
        # third bar gaps above bars[0].high (=2002): low=2010
        bars.append(_bar(t0 + timedelta(minutes=30), o=2010, h=2015, l=2010, c=2014))
        # bunch more bars that don't trade back into the gap
        for i in range(3, 30):
            bars.append(_bar(t0 + timedelta(minutes=15 * i),
                             o=2014 + i, h=2016 + i, l=2013 + i, c=2015 + i))
        fvgs = find_fvgs(bars)
        bull = [z for z in fvgs if z.kind == "fvg_bull"]
        self.assertGreaterEqual(len(bull), 1)
        # The first one should be the synthetic gap
        z = bull[0]
        self.assertAlmostEqual(z.top, 2010.0)
        self.assertAlmostEqual(z.bot, 2002.0)
        self.assertEqual(z.side, "long")
        self.assertEqual(z.status, "active")

    def test_find_fvgs_mitigated_when_wick_returns(self) -> None:
        t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
        bars = [
            _bar(t0, 2000, 2002, 1999, 2001),
            _bar(t0 + timedelta(minutes=15), 2001, 2003, 2000, 2002),
            _bar(t0 + timedelta(minutes=30), 2010, 2015, 2010, 2014),  # bull gap
            # later bar wick re-enters the box but doesn't close through
            _bar(t0 + timedelta(minutes=45), 2014, 2014, 2003, 2012),
            _bar(t0 + timedelta(minutes=60), 2012, 2014, 2011, 2013),
        ]
        fvgs = [z for z in find_fvgs(bars) if z.kind == "fvg_bull"]
        self.assertEqual(len(fvgs), 1)
        self.assertEqual(fvgs[0].status, "mitigated")

    def test_inversion_fvgs(self) -> None:
        t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
        bars = [
            _bar(t0 + timedelta(minutes=15 * i), 2000, 2002, 1999, 2001)
            for i in range(2)
        ]
        bars.append(_bar(t0 + timedelta(minutes=30), 2010, 2015, 2010, 2014))  # bull gap
        # Strong close back below the gap bottom -> inverts to bearish
        for i in range(3, 8):
            bars.append(_bar(t0 + timedelta(minutes=15 * i), 2014, 2014, 1990, 1995))
        ifvgs = find_inversion_fvgs(bars)
        # Must contain at least one bearish IFVG
        bear = [z for z in ifvgs if z.kind == "ifvg_bear"]
        self.assertGreaterEqual(len(bear), 1)
        self.assertEqual(bear[0].side, "short")

    def test_swing_pivots(self) -> None:
        t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
        bars = []
        # build a clear high in the middle
        for i, h in enumerate([100, 101, 102, 105, 102, 101, 100]):
            bars.append(_bar(t0 + timedelta(minutes=15 * i),
                             o=h - 0.5, h=h, l=h - 1, c=h - 0.2))
        pivots = find_swing_pivots(bars, pivot_window=2)
        highs = [z for z in pivots if z.kind == "swing_high"]
        self.assertTrue(any(z.top == 105.0 for z in highs))

    def test_prev_day_levels(self) -> None:
        t0 = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        bars = []
        # day 1
        for i in range(24):
            bars.append(_bar(t0 + timedelta(hours=i),
                             o=100, h=100 + i, l=99, c=100 + i))
        # day 2
        for i in range(5):
            bars.append(_bar(datetime(2026, 5, 2, i, tzinfo=timezone.utc),
                             o=120, h=121, l=119, c=120))
        levels = find_prev_day_levels(bars)
        kinds = {z.kind for z in levels}
        self.assertIn("pdh", kinds)
        self.assertIn("pdl", kinds)
        pdh = next(z for z in levels if z.kind == "pdh")
        self.assertAlmostEqual(pdh.top, 100 + 23)

    def test_asian_range(self) -> None:
        t0 = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        bars = []
        for i in range(8):
            bars.append(_bar(t0 + timedelta(hours=i),
                             o=2000, h=2010 + i, l=1990 - i, c=2000,
                             session="asia"))
        for i in range(4):
            bars.append(_bar(t0 + timedelta(hours=8 + i),
                             o=2000, h=2005, l=1995, c=2000, session="london"))
        zones = find_asian_range(bars)
        self.assertEqual(len(zones), 2)
        kinds = {z.kind for z in zones}
        self.assertEqual(kinds, {"asian_high", "asian_low"})

    def test_all_zones_filters(self) -> None:
        t0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
        bars = [
            _bar(t0 + timedelta(minutes=15 * i),
                 o=2000 + i, h=2002 + i, l=1999 + i, c=2001 + i)
            for i in range(20)
        ]
        zones_all = all_zones(bars)
        zones_only_swings = all_zones(bars, families=["swings"])
        self.assertTrue(all(z.family == "swings" for z in zones_only_swings))
        self.assertGreaterEqual(len(zones_all), len(zones_only_swings))


if __name__ == "__main__":
    unittest.main()
