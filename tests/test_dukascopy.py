from __future__ import annotations

import lzma
import struct
import unittest
from datetime import datetime, timezone

from gold_trader.data.dukascopy import aggregate_ticks_to_bars, decode_dukascopy_ticks


class DukascopyTests(unittest.TestCase):
    def test_decode_dukascopy_ticks(self) -> None:
        records = [
            (1_000, 2_064_000, 2_063_600, 0.2, 0.1),
            (120_000, 2_064_300, 2_063_900, 0.3, 0.2),
        ]
        payload = lzma.compress(b"".join(struct.pack(">iiiff", *record) for record in records))

        ticks = decode_dukascopy_ticks(
            payload=payload,
            base_hour=datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc),
            price_decimals=3,
        )

        self.assertEqual(len(ticks), 2)
        self.assertAlmostEqual(ticks[0].ask, 2064.0)
        self.assertAlmostEqual(ticks[0].bid, 2063.6)
        self.assertEqual(ticks[1].timestamp.minute, 2)

    def test_aggregate_ticks_to_bars(self) -> None:
        records = [
            (1_000, 2_064_000, 2_063_600, 0.2, 0.1),
            (600_000, 2_064_100, 2_063_700, 0.1, 0.1),
            (1_200_000, 2_064_300, 2_063_900, 0.3, 0.2),
        ]
        payload = lzma.compress(b"".join(struct.pack(">iiiff", *record) for record in records))
        ticks = decode_dukascopy_ticks(
            payload=payload,
            base_hour=datetime(2024, 1, 2, 0, 0, tzinfo=timezone.utc),
            price_decimals=3,
        )

        bars = aggregate_ticks_to_bars(ticks, interval_minutes=15)

        self.assertEqual(len(bars), 2)
        self.assertAlmostEqual(bars[0].open, 2063.8)
        self.assertAlmostEqual(bars[0].close, 2063.9)
        self.assertAlmostEqual(bars[1].open, 2064.1)
        self.assertEqual(bars[0].session, "asia")


if __name__ == "__main__":
    unittest.main()