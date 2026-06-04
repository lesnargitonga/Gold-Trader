"""Final setup grading — IFVG technical score + external research context.

External research is a grading layer, not a trade-elimination layer (soft mode default).
Only hard mode may set externally_blocked on the setup.
"""
from __future__ import annotations

from typing import Any

LETTER_THRESHOLDS = (
    (80, "A"),
    (65, "B"),
    (50, "C"),
    (0, "D"),
)

RISK_BY_LETTER = {
    "A": 0.01,
    "B": 0.005,
    "C": 0.0025,
    "D": 0.0,
}

ACTION_BY_LETTER = {
    "A": "Take normal risk after price confirmation",
    "B": "Valid — reduce size or wait for cleaner 5M/1M rejection",
    "C": "Watch only — manual entry only if the chart is very clean",
    "D": "Avoid unless there is a very strong discretionary reason",
}


def letter_from_score(score: int) -> str:
    s = max(0, min(100, int(score)))
    for threshold, letter in LETTER_THRESHOLDS:
        if s >= threshold:
            return letter
    return "D"


def external_confirmation_label(result: Any | None, *, mode: str) -> str:
    if mode == "off" or result is None:
        return "off"
    if result.supports_trade and int(result.confidence or 0) >= 60:
        return "supportive"
    if result.should_block_trade or result.news_risk == "high":
        return "mixed"
    if result.bias in {"mixed", "unknown"}:
        return "mixed"
    return "neutral"


def compute_setup_grading(
    technical_score: int,
    external: Any | None,
    *,
    research_config: Any | None = None,
) -> dict[str, Any]:
    """Combine IFVG technical score with external research into final A/B/C/D grade."""
    from ..research.realtime_research import OpenAIResearchConfig

    cfg = research_config or OpenAIResearchConfig()
    mode = cfg.mode if cfg.mode in {"off", "soft", "hard"} else "soft"
    tech = max(0, min(100, int(technical_score)))
    technical_letter = letter_from_score(tech)
    ext_label = external_confirmation_label(external, mode=mode)

    adjustment = 0
    warnings: list[str] = []
    if external is not None and mode != "off":
        conf = int(external.confidence or 0)
        if ext_label == "supportive":
            adjustment += min(5, max(2, conf // 20))
        elif ext_label == "mixed":
            adjustment -= 5
            if external.news_risk == "high":
                adjustment -= 5
                warnings.append("High-impact news risk — wait until after release if possible")
            if external.should_block_trade:
                warnings.append("External context mixed or opposing — grade reduced, not removed")
        elif external.should_block_trade:
            adjustment -= 7
            warnings.append("External data weakens this direction")
        warnings.extend(list(external.warnings or [])[:6])

    final_score = max(0, min(100, tech + adjustment))
    final_letter = letter_from_score(final_score)
    action = ACTION_BY_LETTER[final_letter]

    if external is not None and external.news_risk == "high" and mode != "off":
        if final_letter == "A":
            final_letter = "B"
            action = "Wait until after high-impact news, then take reduced risk"
        elif final_letter == "B":
            final_letter = "C"
            action = "High news risk — wait until after release unless chart is exceptional"

    return {
        "technical_score": tech,
        "technical_letter": technical_letter,
        "external_confirmation": ext_label,
        "external_adjustment": adjustment,
        "final_score": final_score,
        "letter": final_letter,
        "action": action,
        "suggested_risk_pct": RISK_BY_LETTER.get(final_letter, 0.0),
        "external_warnings": warnings,
        "research_mode": mode,
    }


def apply_soft_mode_external(
    *,
    external: Any | None,
    research_config: Any,
    side: str,
    warnings: list[str],
) -> tuple[bool, list[str]]:
    """Soft/off: never block. Hard: may block via should_external_block."""
    from ..research.realtime_research import should_external_block

    out = list(warnings)
    if external is None or research_config.mode == "off":
        return False, out
    if research_config.mode == "hard":
        blocked = should_external_block(external, side, research_config)
        if blocked:
            out.append("external_research:externally_blocked")
        return blocked, out
    if external.should_block_trade:
        out.append("external_research:context_opposes")
    if external.news_risk == "high":
        out.append("external_research:high_news_risk")
    for w in external.warnings:
        tag = str(w).strip()
        if tag and tag not in out:
            out.append(tag)
    return False, out
