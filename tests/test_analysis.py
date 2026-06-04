from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gold_trader.data.dukascopy import resample_bars
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.research import analyze_timeframe_bundle, write_bundle_analysis_report


class AnalysisTests(unittest.TestCase):
    def test_analyze_timeframe_bundle_returns_profiles(self) -> None:
        bars_15 = generate_synthetic_bars(count=500, seed=31)
        bars_60 = resample_bars(bars_15, interval_minutes=60)

        analysis = analyze_timeframe_bundle({15: bars_15, 60: bars_60})

        self.assertEqual(len(analysis.profiles), 2)
        self.assertIn(analysis.alignment_label, {
            "fully aligned bullish",
            "fully aligned bearish",
            "compression-heavy mixed stack",
            "mixed bullish bias",
            "mixed bearish bias",
            "balanced mixed stack",
        })

    def test_write_bundle_analysis_report_creates_files(self) -> None:
        bars_15 = generate_synthetic_bars(count=500, seed=41)
        bars_60 = resample_bars(bars_15, interval_minutes=60)
        analysis = analyze_timeframe_bundle({15: bars_15, 60: bars_60})

        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = write_bundle_analysis_report(
                datasets={15: bars_15, 60: bars_60},
                analysis=analysis,
                output_dir=temp_dir,
                include_charts=True,
            )

            self.assertTrue(report_path.exists())
            self.assertTrue((Path(temp_dir) / "charts" / "overview.png").exists())
            self.assertTrue((Path(temp_dir) / "charts" / "timeframe_15m.png").exists())


if __name__ == "__main__":
    unittest.main()