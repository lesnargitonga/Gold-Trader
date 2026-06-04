"""8-step operator workflow tests."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from gold_trader.assistants.ifvg_scout import build_approval_brief
from gold_trader.assistants.ifvg_workflow import (
    WORKFLOW_FORMULA,
    build_workflow_context,
    classify_entry_type,
    evaluate_live_sentiment,
    price_in_zone_position,
)
from gold_trader.models import MarketBar


def _bar(close: float, *, high: float | None = None, low: float | None = None) -> MarketBar:
    h = high if high is not None else close + 1
    lo = low if low is not None else close - 1
    return MarketBar(
        timestamp=datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc),
        open=close,
        high=h,
        low=lo,
        close=close,
        volume=100,
        spread=0.1,
    )


class WorkflowTests(unittest.TestCase):
    def test_formula_constant(self) -> None:
        self.assertIn("HTF bias", WORKFLOW_FORMULA)
        self.assertIn("invalidation", WORKFLOW_FORMULA)

    def test_price_in_zone_positions(self) -> None:
        self.assertEqual(price_in_zone_position(100.0, 100.0, 110.0), "bottom")
        self.assertEqual(price_in_zone_position(109.0, 100.0, 110.0), "top")
        self.assertEqual(price_in_zone_position(105.0, 100.0, 110.0), "middle")

    def test_workflow_has_eight_steps_without_setup(self) -> None:
        bars = [_bar(2400 + i * 0.5) for i in range(300)]
        ctx = build_workflow_context(None, primary_bars=bars)
        self.assertEqual(len(ctx["steps"]), 8)
        self.assertEqual(ctx["steps"][0]["step"], 1)
        self.assertIn("combined", ctx["htf_bias"])

    def test_brief_includes_workflow_steps(self) -> None:
        setup = {
            "verdict": "valid_entry",
            "externally_blocked": False,
            "side": "short",
            "score": 90,
            "grade": "A",
            "trade_style": "intraday",
            "zone": {"bot": 2650.0, "top": 2655.0},
            "entry_plan": {"stop": 2660, "invalidation": 2660, "tp1": 2640, "tp2": 2630, "tp3": 2620},
            "checklist": [
                {"name": "retest_rejection", "label": "Retest", "status": "pass", "points": 15, "max_points": 15, "reason": "ok"},
                {"name": "inversion", "label": "Inversion", "status": "pass", "points": 20, "max_points": 20, "reason": "ok"},
            ],
            "warnings": [],
            "external_research": {"enabled": False},
        }
        bars = [_bar(2652.0) for _ in range(300)]
        workflow = build_workflow_context(setup, primary_bars=bars, current_price=2652.0)
        brief = build_approval_brief(setup, workflow)
        self.assertEqual(len(brief["workflow_steps"]), 8)
        self.assertIn("formula", brief)

    def test_entry_type_pullback(self) -> None:
        setup = {
            "side": "short",
            "checklist": [
                {"name": "retest_rejection", "status": "pass"},
                {"name": "inversion", "status": "pass"},
            ],
        }
        kind, _ = classify_entry_type(setup, price_position="middle", ltf_status="pass")
        self.assertEqual(kind, "pullback")

    def test_macro_aligned_blocked(self) -> None:
        out = evaluate_live_sentiment(
            side="short",
            alignment="mixed bearish bias",
            macro_regime="aligned",
            macro_override=False,
        )
        self.assertTrue(any("macro_regime=aligned" in b for b in out["blockers"]))
        self.assertEqual(out["warnings"], [])

    def test_macro_mixed_allowed(self) -> None:
        out = evaluate_live_sentiment(
            side="short",
            alignment="mixed bearish bias",
            macro_regime="mixed",
            macro_override=False,
        )
        self.assertEqual(out["blockers"], [])

    def test_macro_override_softens_block(self) -> None:
        out = evaluate_live_sentiment(
            side="short",
            alignment="mixed bearish bias",
            macro_regime="aligned",
            macro_override=True,
        )
        self.assertEqual(out["blockers"], [])
        self.assertTrue(any("IFVG_MACRO_OVERRIDE" in w for w in out["warnings"]))


if __name__ == "__main__":
    unittest.main()
