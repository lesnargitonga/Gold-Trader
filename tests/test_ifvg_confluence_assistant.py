from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from pathlib import Path

from gold_trader.assistants.ifvg_confluence import (
    IFVGAssistantConfig,
    MarketLevel,
    find_ifvg_setups,
    record_shadow_setup,
    setup_to_dict,
)
from gold_trader.research.realtime_research import (
    OpenAIResearchConfig,
    RealtimeResearchResult,
)
from gold_trader.models import MarketBar, Side
from gold_trader.research import build_bundle_snapshot


def _bar(ts: datetime, o: float, h: float, l: float, c: float) -> MarketBar:
    return MarketBar(timestamp=ts, open=o, high=h, low=l, close=c, spread=0.2, volume=100)


def _series(rows: list[tuple[float, float, float, float]]) -> list[MarketBar]:
    ts = datetime(2026, 5, 18, 13, 0, tzinfo=timezone.utc)
    out = []
    for row in rows:
        out.append(_bar(ts, *row))
        ts += timedelta(minutes=15)
    return out


def _bearish_ifvg_short_rows(*, weak_retest: bool = False) -> list[tuple[float, float, float, float]]:
    rows: list[tuple[float, float, float, float]] = []
    for _ in range(20):
        rows.append((100.0, 101.0, 99.0, 100.0))
    rows.extend([
        (100.0, 101.0, 99.0, 100.0),
        (100.2, 100.8, 99.2, 100.1),
        (100.0, 102.4, 99.8, 100.2),   # pivot high
        (100.1, 100.8, 99.7, 100.0),
        (100.0, 100.7, 99.6, 100.1),
        (100.2, 103.0, 99.8, 100.0),   # sweep high and reclaim before IFVG
        (100.0, 101.0, 99.0, 100.0),   # k-2 high 101
        (100.0, 112.0, 99.5, 111.0),   # impulse up
        (111.0, 113.0, 110.0, 112.0),  # k low 110 -> bullish FVG 101-110
        (112.0, 112.5, 111.0, 111.5),
        (111.5, 112.0, 99.5, 100.0),   # inversion close below 101
    ])
    if weak_retest:
        rows.append((100.8, 101.2, 100.2, 101.1))
    else:
        rows.append((100.8, 101.5, 99.8, 100.0))  # retest rejection
    return rows


class IFVGConfluenceAssistantTests(unittest.TestCase):
    def test_detects_sequence_valid_short_setup(self) -> None:
        bars = _series(_bearish_ifvg_short_rows())
        setups = find_ifvg_setups(
            bars,
            config=IFVGAssistantConfig(htf_slow=2, htf_fast=1),
            higher_timeframe_bias="bearish",
        )
        self.assertTrue(setups)
        top = setups[0]
        self.assertEqual(top.candidate.side, Side.SHORT)
        self.assertEqual(top.candidate.source, "inverted_bullish_fvg")
        self.assertGreaterEqual(top.score, 65)
        self.assertEqual(top.manual_approval_required, True)
        self.assertGreater(top.plan.stop, top.plan.entry)

    def test_setup_to_dict_labels_inverted_bullish_fvg_as_sell(self) -> None:
        bars = _series(_bearish_ifvg_short_rows())
        setup = find_ifvg_setups(
            bars,
            config=IFVGAssistantConfig(htf_slow=2, htf_fast=1),
            higher_timeframe_bias="bearish",
        )[0]
        d = setup_to_dict(setup, timeframe_minutes=15)
        self.assertEqual(d["side"], "short")
        self.assertEqual(d["trade_action"], "sell")
        self.assertEqual(d["source"], "inverted_bullish_fvg")
        self.assertIn("SELL", d["inversion_note"])
        self.assertIn("bullish FVG", d["inversion_note"])

    def test_missing_macro_and_levels_are_neutral(self) -> None:
        bars = _series(_bearish_ifvg_short_rows())
        setup = find_ifvg_setups(
            bars,
            config=IFVGAssistantConfig(htf_slow=2, htf_fast=1),
            higher_timeframe_bias="bearish",
        )[0]
        items = {item.name: item for item in setup.checklist}
        self.assertEqual(items["macro_confirmation"].status, "partial")
        self.assertEqual(items["not_into_sr"].status, "partial")

    def test_level_ahead_reduces_score(self) -> None:
        bars = _series(_bearish_ifvg_short_rows())
        setup = find_ifvg_setups(
            bars,
            config=IFVGAssistantConfig(htf_slow=2, htf_fast=1),
            higher_timeframe_bias="bearish",
            market_levels=[MarketLevel(price=99.8, kind="support", label="near support")],
        )[0]
        items = {item.name: item for item in setup.checklist}
        self.assertEqual(items["not_into_sr"].status, "fail")

    def test_retest_without_rejection_is_not_valid_entry(self) -> None:
        bars = _series(_bearish_ifvg_short_rows(weak_retest=True))
        setups = find_ifvg_setups(
            bars,
            config=IFVGAssistantConfig(htf_slow=2, htf_fast=1),
            higher_timeframe_bias="bearish",
        )
        self.assertFalse(setups)

    def test_agent_cycle_includes_ifvg_candidate(self) -> None:
        bars = _series(_bearish_ifvg_short_rows())
        snapshot = build_bundle_snapshot(
            datasets={15: bars, 60: bars},
            families=["inversion_fair_value_gap"],
            max_candidates=5,
        )
        self.assertTrue(any(c.family == "inversion_fair_value_gap" for c in snapshot.entry_candidates))

    def test_shadow_writer_creates_csv(self) -> None:
        bars = _series(_bearish_ifvg_short_rows())
        setup = find_ifvg_setups(
            bars,
            config=IFVGAssistantConfig(htf_slow=2, htf_fast=1),
            higher_timeframe_bias="bearish",
        )[0]
        with TemporaryDirectory() as td:
            path = Path(td) / "shadow.csv"
            record_shadow_setup(path, setup, timeframe_minutes=15)
            text = path.read_text()
            self.assertIn("outcome_r", text)
            self.assertIn("inversion", text)
            self.assertIn("external_bias", text)

    def test_hard_mode_can_mark_setup_externally_blocked(self) -> None:
        bars = _series(_bearish_ifvg_short_rows())
        external = RealtimeResearchResult(
            timestamp_utc="now",
            symbol="XAUUSD",
            bias="bullish_gold",
            supports_trade=False,
            should_block_trade=True,
            confidence=80,
            news_risk="medium",
            macro={"dxy_bias": "supports_buy", "us10y_bias": "supports_buy", "real_yield_bias": "unknown"},
            options={"bias": "unknown", "important_levels": [], "danger_zones": [], "notes": ""},
            warnings=["external opposition"],
            summary="Opposes sell.",
            sources=[],
        )
        with unittest.mock.patch(
            "gold_trader.research.realtime_research.run_openai_market_research",
            return_value=external,
        ):
            setups = find_ifvg_setups(
                bars,
                config=IFVGAssistantConfig(htf_slow=2, htf_fast=1),
                higher_timeframe_bias="bearish",
                openai_research_config=OpenAIResearchConfig(
                    enabled=True,
                    mode="hard",
                    min_ifvg_score_to_research=65,
                ),
            )
        self.assertTrue(setups[0].externally_blocked)
        self.assertEqual(setups[0].verdict, "externally_blocked")

    def test_no_ifvg_candidate_does_not_call_openai(self) -> None:
        bars = _series([(100.0, 101.0, 99.0, 100.0)] * 40)
        with unittest.mock.patch(
            "gold_trader.research.realtime_research.run_openai_market_research",
            side_effect=AssertionError("must not call"),
        ):
            setups = find_ifvg_setups(
                bars,
                config=IFVGAssistantConfig(htf_slow=2, htf_fast=1),
                openai_research_config=OpenAIResearchConfig(enabled=True, mode="soft"),
            )
        self.assertFalse(setups)


if __name__ == "__main__":
    unittest.main()
