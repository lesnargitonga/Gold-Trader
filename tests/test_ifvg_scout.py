"""IFVG auto-scout and approval brief tests."""
from __future__ import annotations

import unittest

from gold_trader.assistants.ifvg_scout import build_approval_brief, scout_public_view


def _live_workflow(**overrides: object) -> dict:
    base = {
        "workflow_ready": True,
        "steps": [],
        "formula": "HTF → zone → price → LTF → macro → entry",
        "htf_bias": {"combined": "bearish"},
        "live_sentiment": {
            "alignment": "mixed bearish bias",
            "macro_regime": "mixed",
            "blockers": [],
            "warnings": [],
        },
    }
    base.update(overrides)
    return base


class ApprovalBriefTests(unittest.TestCase):
    def test_no_setup_not_enter(self) -> None:
        brief = build_approval_brief(None)
        self.assertFalse(brief["can_enter"])
        self.assertIn("watching", brief["headline"].lower())

    def test_valid_entry_can_enter(self) -> None:
        from gold_trader.assistants.ifvg_grading import compute_setup_grading
        from gold_trader.research.realtime_research import OpenAIResearchConfig

        grading = compute_setup_grading(93, None, research_config=OpenAIResearchConfig(mode="soft"))
        setup = {
            "verdict": "valid_entry",
            "externally_blocked": False,
            "side": "short",
            "score": 93,
            "grade": grading["letter"],
            "grading": grading,
            "trade_style": "swing",
            "ai_verdict": "TAKE — SHORT swing IFVG · grade A (tech 93/100).",
            "zone": {"bot": 4540.0, "top": 4557.0},
            "entry_plan": {"entry_low": 4540, "entry_high": 4557, "stop": 4568, "tp1": 4525},
            "checklist": [
                {"label": "Liquidity sweep", "points": 20, "max_points": 20, "status": "pass", "reason": "ok"},
            ],
            "warnings": [],
            "external_research": {"enabled": True, "mode": "soft", "supports_trade": True, "should_block_trade": False, "confidence": 72, "macro": {}, "options": {"bias": "bearish_gold", "important_levels": [4550]}},
        }
        brief = build_approval_brief(setup, _live_workflow())
        self.assertTrue(brief["can_enter"])
        self.assertEqual(brief["blockers"], [])
        self.assertEqual(brief["final_grade"], "A")
        self.assertEqual(brief["suggested_risk_usd"], 30.0)
        self.assertTrue(brief["manual_approval_required"])
        self.assertIn("bundle accept", brief["summary"].lower())

    def test_approval_brief_includes_buy_sell_inversion_context(self) -> None:
        setup = {
            "verdict": "alert_wait",
            "externally_blocked": False,
            "side": "short",
            "score": 70,
            "grade": "B",
            "grading": {"letter": "B", "action": "Wait"},
            "zone": {"bot": 4540.0, "top": 4557.0},
            "entry_plan": {"stop": 4568, "tp1": 4525},
            "inversion_note": "Former bullish FVG inverted — zone is now resistance (SELL on retest)",
            "checklist": [],
            "warnings": [],
            "external_research": {"enabled": False, "mode": "soft"},
        }
        brief = build_approval_brief(setup, _live_workflow(workflow_ready=False))
        joined = " ".join(brief["model_watch"])
        self.assertIn("SELL IFVG zone", joined)
        self.assertIn("SELL on retest", joined)

    def test_grade_b_blocked_for_live(self) -> None:
        setup = {
            "verdict": "valid_entry",
            "externally_blocked": False,
            "side": "short",
            "score": 78,
            "grade": "B",
            "grading": {"letter": "B", "action": "Valid — reduce size"},
            "checklist": [],
            "warnings": [],
            "external_research": {"enabled": False, "mode": "soft"},
        }
        brief = build_approval_brief(setup, _live_workflow(workflow_ready=False))
        self.assertFalse(brief["can_enter"])
        self.assertTrue(any("grade b" in b.lower() and "audit" in b.lower() for b in brief["blockers"]))

    def test_grade_c_blocked_for_live(self) -> None:
        setup = {
            "verdict": "alert_wait",
            "externally_blocked": False,
            "side": "long",
            "score": 55,
            "grade": "C",
            "grading": {"letter": "C", "action": "Watch only"},
            "checklist": [],
            "warnings": [],
            "external_research": {"enabled": False, "mode": "soft"},
        }
        brief = build_approval_brief(setup, _live_workflow(workflow_ready=False))
        self.assertFalse(brief["can_enter"])
        self.assertTrue(any("grade c" in b.lower() for b in brief["blockers"]))

    def test_alignment_blocker(self) -> None:
        setup = {
            "verdict": "valid_entry",
            "externally_blocked": False,
            "side": "short",
            "score": 90,
            "grade": "A",
            "grading": {"letter": "A", "action": "Take normal risk"},
            "checklist": [],
            "warnings": [],
            "external_research": {"enabled": False, "mode": "soft"},
        }
        wf = _live_workflow(
            live_sentiment={
                "alignment": "mixed bullish bias",
                "macro_regime": "mixed",
                "blockers": ["Alignment «mixed bullish bias» — live profile requires mixed bearish bias (30-day audit)"],
                "warnings": [],
            },
        )
        brief = build_approval_brief(setup, wf)
        self.assertFalse(brief["can_enter"])
        self.assertTrue(any("mixed bullish" in b for b in brief["blockers"]))

    def test_macro_aligned_blocked_for_live(self) -> None:
        setup = {
            "verdict": "valid_entry",
            "externally_blocked": False,
            "side": "short",
            "score": 90,
            "grade": "A",
            "grading": {"letter": "A", "action": "Take normal risk"},
            "zone": {"bot": 4540.0, "top": 4557.0},
            "entry_plan": {"stop": 4568, "tp1": 4525, "tp2": 4510, "tp3": 4480},
            "checklist": [],
            "warnings": [],
            "external_research": {"enabled": False, "mode": "soft"},
        }
        wf = _live_workflow(
            live_sentiment={
                "alignment": "mixed bearish bias",
                "macro_regime": "aligned",
                "blockers": [
                    "macro_regime=aligned — live profile requires macro_regime=mixed "
                    "(30-day audit: aligned −57R; set IFVG_MACRO_OVERRIDE=1 to bypass)"
                ],
                "warnings": [],
            },
        )
        brief = build_approval_brief(setup, wf)
        self.assertFalse(brief["can_enter"])
        self.assertTrue(any("macro_regime=aligned" in b for b in brief["blockers"]))

    def test_blocked_externally_hard_mode_only(self) -> None:
        from gold_trader.assistants.ifvg_grading import compute_setup_grading
        from gold_trader.research.realtime_research import OpenAIResearchConfig

        grading = compute_setup_grading(90, None, research_config=OpenAIResearchConfig(mode="hard"))
        setup = {
            "verdict": "externally_blocked",
            "externally_blocked": True,
            "side": "long",
            "score": 90,
            "grade": "D",
            "grading": {**grading, "letter": "D"},
            "checklist": [],
            "warnings": ["external_research:externally_blocked"],
            "external_research": {"enabled": True, "mode": "hard", "should_block_trade": True},
        }
        brief = build_approval_brief(setup)
        self.assertFalse(brief["can_enter"])


class ScoutPublicViewTests(unittest.TestCase):
    def test_public_shape(self) -> None:
        view = scout_public_view({"status": "scanning", "model_alerts": [], "approval_brief": build_approval_brief(None)})
        self.assertIn("approval_brief", view)
        self.assertIn("model_alerts", view)


if __name__ == "__main__":
    unittest.main()
