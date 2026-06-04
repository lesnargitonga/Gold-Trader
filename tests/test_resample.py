from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from gold_trader.data.dukascopy import resample_bars
from gold_trader.models import MarketBar


class ResampleTests(unittest.TestCase):
    def test_resample_bars_preserves_ohlc_alignment(self) -> None:
        start = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        bars = []
        closes = [2000.1, 2000.4, 1999.8, 2000.9, 2001.2]
        highs = [2000.2, 2000.5, 2000.0, 2001.0, 2001.4]
        lows = [1999.9, 2000.0, 1999.7, 2000.3, 2000.7]
        spreads = [0.20, 0.25, 0.30, 0.35, 0.40]

        for index in range(5):
            bars.append(
                MarketBar(
                    timestamp=start + timedelta(minutes=index),
                    open=2000.0 + index * 0.1,
                    high=highs[index],
                    low=lows[index],
                    close=closes[index],
                    volume=10.0 + index,
                    spread=spreads[index],
                    session="new_york",
                )
            )

        resampled = resample_bars(bars, interval_minutes=5)

        self.assertEqual(len(resampled), 1)
        self.assertEqual(resampled[0].timestamp, start)
        self.assertAlmostEqual(resampled[0].open, 2000.0)
        self.assertAlmostEqual(resampled[0].high, 2001.4)
        self.assertAlmostEqual(resampled[0].low, 1999.7)
        self.assertAlmostEqual(resampled[0].close, 2001.2)
        self.assertAlmostEqual(resampled[0].volume, sum(10.0 + index for index in range(5)))
        self.assertAlmostEqual(resampled[0].spread, sum(spreads) / len(spreads))


if __name__ == "__main__":
    unittest.main()