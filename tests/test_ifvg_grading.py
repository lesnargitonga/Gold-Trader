"""External research grading layer tests."""
from __future__ import annotations

import unittest

from gold_trader.assistants.ifvg_grading import compute_setup_grading, letter_from_score
from gold_trader.assistants.ifvg_scout import build_approval_brief
from gold_trader.research.realtime_research import OpenAIResearchConfig, RealtimeResearchResult


def _live_workflow() -> dict:
    return {
        "workflow_ready": True,
        "steps": [],
        "live_sentiment": {"alignment": "mixed bearish bias", "macro_regime": "mixed", "blockers": [], "warnings": []},
    }


def _external(*, supports: bool = False, block: bool = False, conf: int = 50, news: str = "low") -> RealtimeResearchResult:
    return RealtimeResearchResult(
        timestamp_utc="now",
        symbol="XAUUSD",
        bias="mixed",
        supports_trade=supports,
        should_block_trade=block,
        confidence=conf,
        news_risk=news,
        macro={"dxy_bias": "neutral", "us10y_bias": "neutral", "real_yield_bias": "unknown"},
        options={"bias": "neutral", "important_levels": [], "danger_zones": [], "notes": ""},
        warnings=[],
        summary="mixed context",
        sources=[],
    )


class GradingTests(unittest.TestCase):
    def test_letter_thresholds(self) -> None:
        self.assertEqual(letter_from_score(85), "A")
        self.assertEqual(letter_from_score(78), "B")
        self.assertEqual(letter_from_score(55), "C")
        self.assertEqual(letter_from_score(40), "D")

    def test_mixed_external_keeps_b_grade(self) -> None:
        g = compute_setup_grading(
            78,
            _external(block=True, conf=40),
            research_config=OpenAIResearchConfig(enabled=True, mode="soft"),
        )
        self.assertEqual(g["technical_score"], 78)
        self.assertEqual(g["external_confirmation"], "mixed")
        self.assertEqual(g["letter"], "B")

    def test_supportive_external_can_reach_a(self) -> None:
        g = compute_setup_grading(
            85,
            _external(supports=True, conf=75),
            research_config=OpenAIResearchConfig(enabled=True, mode="soft"),
        )
        self.assertEqual(g["letter"], "A")
        self.assertEqual(g["external_confirmation"], "supportive")

    def test_high_news_downgrades_in_soft(self) -> None:
        g = compute_setup_grading(
            82,
            _external(news="high", block=True),
            research_config=OpenAIResearchConfig(enabled=True, mode="soft"),
        )
        self.assertIn(g["letter"], ("B", "C"))
        self.assertTrue(any("news" in w.lower() for w in g["external_warnings"]))

    def test_soft_mode_brief_does_not_block_on_external(self) -> None:
        setup = {
            "verdict": "valid_entry",
            "externally_blocked": False,
            "side": "long",
            "score": 85,
            "grade": "A",
            "trade_style": "intraday",
            "grading": compute_setup_grading(
                85,
                _external(block=True, news="low"),
                research_config=OpenAIResearchConfig(enabled=True, mode="soft"),
            ),
            "checklist": [],
            "warnings": ["external_research:context_opposes"],
            "external_research": {"enabled": True, "mode": "soft", "should_block_trade": True, "news_risk": "low"},
            "entry_plan": {"stop": 1, "tp1": 2},
            "zone": {"bot": 1, "top": 2},
        }
        brief = build_approval_brief(setup, _live_workflow())
        self.assertFalse(setup["externally_blocked"])
        self.assertNotIn("External research blocks", " ".join(brief.get("blockers") or []))
        if brief["final_grade"] == "A":
            self.assertTrue(brief["can_enter"])
        else:
            self.assertFalse(brief["can_enter"])


if __name__ == "__main__":
    unittest.main()
