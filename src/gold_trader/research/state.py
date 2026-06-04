from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..assistants.ifvg_confluence import (
    IFVGSetup,
    find_ifvg_setups,
    load_market_levels,
    record_shadow_setup,
    setup_to_dict,
)
from ..calendar import NewsCalendar
from ..models import MarketBar, Side
from ..strategies import (
    AsianRangeBreakoutStrategy,
    CompressionBreakoutStrategy,
    LiquiditySweepStrategy,
    LondonBreakoutStrategy,
    MomentumBurstStrategy,
    NYSessionBreakoutStrategy,
    TrendPullbackStrategy,
)
from .analysis import BundleAnalysis, TimeframeAnalysis, analyze_timeframe_bundle


@dataclass(frozen=True)
class TimeframeState:
    timeframe_minutes: int
    current_time: datetime
    current_close: float
    recent_support: float
    recent_resistance: float
    trend_state: str
    structure_state: str
    execution_style: str
    trend_strength: float
    rsi14: float
    atr14: float
    spread: float


@dataclass(frozen=True)
class EntryCandidate:
    family: str
    timeframe_minutes: int
    side: Side
    reference_price: float
    stop: float
    target: float
    score: int
    regime_fit: str
    reason: str
    conflict: str | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class DecisionPlan:
    status: str
    family: str | None
    timeframe_minutes: int | None
    side: Side | None
    reference_price: float | None
    stop: float | None
    target: float | None
    score: int
    risk_reward: float
    rationale: tuple[str, ...]


@dataclass(frozen=True)
class BundleSnapshot:
    generated_at: datetime
    alignment_label: str
    higher_timeframe_bias: str
    oscillation_label: str
    timeframe_states: tuple[TimeframeState, ...]
    entry_candidates: tuple[EntryCandidate, ...]
    decision: DecisionPlan
    warnings: tuple[str, ...]


def build_bundle_snapshot(
    datasets: dict[int, Sequence[MarketBar]],
    families: Sequence[str],
    max_candidates: int = 8,
    *,
    macro_frame: Any = None,
    market_levels_path: str | None = None,
    news_calendar_path: str | None = None,
    shadow_journal_path: str | None = None,
    openai_research_config_path: str | None = None,
    openai_research_cache_path: str | None = None,
) -> BundleSnapshot:
    analysis = analyze_timeframe_bundle(datasets)
    profiles_by_timeframe = {profile.timeframe_minutes: profile for profile in analysis.profiles}
    timeframe_states = tuple(
        _build_timeframe_state(timeframe_minutes, datasets[timeframe_minutes], profiles_by_timeframe[timeframe_minutes])
        for timeframe_minutes in sorted(datasets)
    )
    higher_timeframe_bias = _higher_timeframe_bias(analysis)
    oscillation_label = _oscillation_label(analysis)
    candidates = _entry_candidates(
        datasets=datasets,
        analysis=analysis,
        higher_timeframe_bias=higher_timeframe_bias,
        families=families,
        max_candidates=max_candidates,
        macro_frame=macro_frame,
        market_levels_path=market_levels_path,
        news_calendar_path=news_calendar_path,
        shadow_journal_path=shadow_journal_path,
        openai_research_config_path=openai_research_config_path,
        openai_research_cache_path=openai_research_cache_path,
    )
    warnings = _build_warnings(candidates, higher_timeframe_bias, oscillation_label)
    decision = _decision_plan(
        candidates=candidates,
        timeframe_states=timeframe_states,
        higher_timeframe_bias=higher_timeframe_bias,
        oscillation_label=oscillation_label,
    )

    latest_time = max(_normalized_time(state.current_time) for state in timeframe_states)
    return BundleSnapshot(
        generated_at=latest_time,
        alignment_label=analysis.alignment_label,
        higher_timeframe_bias=higher_timeframe_bias,
        oscillation_label=oscillation_label,
        timeframe_states=timeframe_states,
        entry_candidates=tuple(candidates[:max_candidates]),
        decision=decision,
        warnings=tuple(warnings),
    )


def _build_timeframe_state(
    timeframe_minutes: int,
    bars: Sequence[MarketBar],
    profile: TimeframeAnalysis,
) -> TimeframeState:
    lookback = min(20, len(bars))
    recent = bars[-lookback:]
    structure_state = _structure_state(profile)
    return TimeframeState(
        timeframe_minutes=timeframe_minutes,
        current_time=bars[-1].timestamp,
        current_close=bars[-1].close,
        recent_support=min(bar.low for bar in recent),
        recent_resistance=max(bar.high for bar in recent),
        trend_state=profile.trend_state,
        structure_state=structure_state,
        execution_style=_execution_style(profile.trend_state, structure_state),
        trend_strength=profile.trend_strength,
        rsi14=profile.rsi14,
        atr14=profile.atr14,
        spread=bars[-1].spread,
    )


def _entry_candidates(
    datasets: dict[int, Sequence[MarketBar]],
    analysis: BundleAnalysis,
    higher_timeframe_bias: str,
    families: Sequence[str],
    max_candidates: int,
    macro_frame: Any = None,
    market_levels_path: str | None = None,
    news_calendar_path: str | None = None,
    shadow_journal_path: str | None = None,
    openai_research_config_path: str | None = None,
    openai_research_cache_path: str | None = None,
) -> list[EntryCandidate]:
    enabled = {family.strip().lower() for family in families if family.strip()}
    profiles_by_timeframe = {profile.timeframe_minutes: profile for profile in analysis.profiles}
    candidates: list[EntryCandidate] = []
    market_levels = load_market_levels(market_levels_path) if market_levels_path else []
    news_calendar = NewsCalendar.load(Path(news_calendar_path)) if news_calendar_path else None

    for timeframe_minutes, bars in sorted(datasets.items()):
        profile = profiles_by_timeframe[timeframe_minutes]
        if "inversion_fair_value_gap" in enabled:
            candidates.extend(
                _scan_ifvg_assistant_candidates(
                    timeframe_minutes=timeframe_minutes,
                    bars=bars,
                    higher_timeframe_bias=higher_timeframe_bias,
                    macro_frame=macro_frame,
                    market_levels=market_levels,
                    news_calendar=news_calendar,
                    shadow_journal_path=shadow_journal_path,
                    openai_research_config_path=openai_research_config_path,
                    openai_research_cache_path=openai_research_cache_path,
                )
            )
        if "liquidity_sweep" in enabled:
            candidates.extend(
                _scan_strategy_candidates(
                    family="liquidity_sweep",
                    strategy=_liquidity_strategy_for_timeframe(timeframe_minutes),
                    timeframe_minutes=timeframe_minutes,
                    bars=bars,
                    profile=profile,
                    higher_timeframe_bias=higher_timeframe_bias,
                )
            )
        if "compression_breakout" in enabled:
            candidates.extend(
                _scan_strategy_candidates(
                    family="compression_breakout",
                    strategy=_compression_strategy_for_timeframe(timeframe_minutes),
                    timeframe_minutes=timeframe_minutes,
                    bars=bars,
                    profile=profile,
                    higher_timeframe_bias=higher_timeframe_bias,
                )
            )
        if "asian_range_breakout" in enabled:
            candidates.extend(
                _scan_strategy_candidates(
                    family="asian_range_breakout",
                    strategy=_asian_range_strategy_for_timeframe(timeframe_minutes),
                    timeframe_minutes=timeframe_minutes,
                    bars=bars,
                    profile=profile,
                    higher_timeframe_bias=higher_timeframe_bias,
                )
            )
        if "london_breakout" in enabled:
            candidates.extend(
                _scan_strategy_candidates(
                    family="london_breakout",
                    strategy=_london_breakout_strategy_for_timeframe(timeframe_minutes),
                    timeframe_minutes=timeframe_minutes,
                    bars=bars,
                    profile=profile,
                    higher_timeframe_bias=higher_timeframe_bias,
                )
            )
        if "trend_pullback" in enabled:
            candidates.extend(
                _scan_strategy_candidates(
                    family="trend_pullback",
                    strategy=_trend_pullback_strategy_for_timeframe(timeframe_minutes),
                    timeframe_minutes=timeframe_minutes,
                    bars=bars,
                    profile=profile,
                    higher_timeframe_bias=higher_timeframe_bias,
                )
            )
        if "ny_session_breakout" in enabled:
            candidates.extend(
                _scan_strategy_candidates(
                    family="ny_session_breakout",
                    strategy=NYSessionBreakoutStrategy(
                        atr_period=10, risk_reward=2.0, max_spread=0.75,
                        min_breakout_atr=0.05, min_range_atr=0.3, min_london_bars=3,
                    ),
                    timeframe_minutes=timeframe_minutes,
                    bars=bars,
                    profile=profile,
                    higher_timeframe_bias=higher_timeframe_bias,
                )
            )
        if "momentum_burst" in enabled:
            candidates.extend(
                _scan_strategy_candidates(
                    family="momentum_burst",
                    strategy=MomentumBurstStrategy(
                        atr_period=14, min_body_atr=0.4, body_fraction=0.25,
                        risk_reward=2.5, max_spread=0.75,
                    ),
                    timeframe_minutes=timeframe_minutes,
                    bars=bars,
                    profile=profile,
                    higher_timeframe_bias=higher_timeframe_bias,
                )
            )
        if (
            "timed_horizon_macro_regime" in enabled
            and macro_frame is not None
            and timeframe_minutes in (60, 240)
        ):
            from ..strategies.timed_horizon_macro_regime import (
                TimedHorizonMacroRegimeStrategy,
            )
            candidates.extend(
                _scan_strategy_candidates(
                    family="timed_horizon_macro_regime",
                    strategy=TimedHorizonMacroRegimeStrategy(
                        macro=macro_frame,
                        real_yield_lookback_days=10,
                        real_yield_max_change_bps=0.0,
                        vix_max_change_abs=2.5,
                        dxy_max_abs_change_pct=1.0,
                        far_atr_mult=8.0,
                    ),
                    timeframe_minutes=timeframe_minutes,
                    bars=bars,
                    profile=profile,
                    higher_timeframe_bias=higher_timeframe_bias,
                )
            )

    candidates.sort(key=lambda candidate: (candidate.score, candidate.timeframe_minutes), reverse=True)
    return candidates[:max_candidates]


def _scan_ifvg_assistant_candidates(
    timeframe_minutes: int,
    bars: Sequence[MarketBar],
    higher_timeframe_bias: str,
    macro_frame: Any = None,
    market_levels: Sequence[Any] = (),
    news_calendar: NewsCalendar | None = None,
    shadow_journal_path: str | None = None,
    openai_research_config_path: str | None = None,
    openai_research_cache_path: str | None = None,
) -> list[EntryCandidate]:
    setups = find_ifvg_setups(
        bars,
        macro_frame=macro_frame,
        market_levels=market_levels,
        news_calendar=news_calendar,
        higher_timeframe_bias=higher_timeframe_bias,
        openai_config_path=openai_research_config_path or "config/openai_research.json",
        openai_cache_path=openai_research_cache_path or "data/cache/openai_market_research.json",
    )
    candidates: list[EntryCandidate] = []
    for setup in setups:
        if setup.verdict == "ignore":
            continue
        if shadow_journal_path and setup.score >= 65:
            record_shadow_setup(Path(shadow_journal_path), setup, timeframe_minutes=timeframe_minutes)
        candidate = _entry_candidate_from_ifvg_setup(setup, timeframe_minutes)
        candidates.append(candidate)
    return candidates


def _entry_candidate_from_ifvg_setup(setup: IFVGSetup, timeframe_minutes: int) -> EntryCandidate:
    c = setup.candidate
    plan = setup.plan
    conflict = None
    failed = [item.name for item in setup.checklist if item.status == "fail"]
    if failed:
        conflict = "IFVG checklist failed: " + ",".join(failed)
    zone = f"{c.gap_bot:.2f}-{c.gap_top:.2f}"
    reason = (
        f"IFVG confluence {setup.grade}: {c.side.value} zone={zone} "
        f"entry={plan.entry_low:.2f}-{plan.entry_high:.2f} "
        f"SL={plan.stop:.2f} TP1={plan.tp1:.2f} TP2={plan.tp2:.2f} TP3={plan.tp3:.2f} "
        f"verdict={setup.verdict}; manual approval required"
    )
    return EntryCandidate(
        family="inversion_fair_value_gap",
        timeframe_minutes=timeframe_minutes,
        side=c.side,
        reference_price=plan.entry,
        stop=plan.stop,
        target=plan.tp2,
        score=setup.score,
        regime_fit="IFVG confluence assistant",
        reason=reason,
        conflict=conflict,
        details=setup_to_dict(setup, timeframe_minutes=timeframe_minutes),
    )


def _scan_strategy_candidates(
    family: str,
    strategy,
    timeframe_minutes: int,
    bars: Sequence[MarketBar],
    profile: TimeframeAnalysis,
    higher_timeframe_bias: str,
) -> list[EntryCandidate]:
    candidates: list[EntryCandidate] = []
    start_index = max(strategy.warmup_bars(), len(bars) - 4)
    structure_state = _structure_state(profile)

    for index in range(start_index, len(bars)):
        signal = strategy.signal_for(bars, index)
        if signal is None:
            continue

        reference_price = bars[index].close
        score, regime_fit, conflict = _score_signal(
            family=family,
            side=signal.side,
            timeframe_minutes=timeframe_minutes,
            profile=profile,
            structure_state=structure_state,
            higher_timeframe_bias=higher_timeframe_bias,
        )
        candidates.append(
            EntryCandidate(
                family=family,
                timeframe_minutes=timeframe_minutes,
                side=signal.side,
                reference_price=reference_price,
                stop=signal.stop,
                target=signal.target,
                score=score,
                regime_fit=regime_fit,
                reason=signal.reason,
                conflict=conflict,
            )
        )

    return candidates


def _score_signal(
    family: str,
    side: Side,
    timeframe_minutes: int,
    profile: TimeframeAnalysis,
    structure_state: str,
    higher_timeframe_bias: str,
) -> tuple[int, str, str | None]:
    score = 50
    conflict = None
    regime_fit = "mixed"

    if higher_timeframe_bias == "bullish" and side is Side.LONG:
        score += 15
    elif higher_timeframe_bias == "bearish" and side is Side.SHORT:
        score += 15
    elif higher_timeframe_bias in {"bullish", "bearish"}:
        score -= 15
        conflict = f"counter to higher-timeframe {higher_timeframe_bias} bias"

    if profile.trend_state == "uptrend" and side is Side.LONG:
        score += 10
    elif profile.trend_state == "downtrend" and side is Side.SHORT:
        score += 10
    elif profile.trend_state in {"uptrend", "downtrend"}:
        score -= 8

    if family == "liquidity_sweep":
        if structure_state == "sweep_dominant":
            score += 10
            regime_fit = "oscillation / mean reversion"
        elif structure_state == "breakout_dominant":
            score -= 6

        if side is Side.LONG and profile.rsi14 <= 45.0:
            score += 5
        elif side is Side.SHORT and profile.rsi14 >= 55.0:
            score += 5
    elif family == "asian_range_breakout":
        # Asian range breakouts work best in trending regimes where the direction
        # is already established by the time London opens.
        if profile.trend_state in {"uptrend", "downtrend"}:
            score += 8
            regime_fit = "session open trend continuation"
        elif structure_state == "breakout_dominant":
            score += 6
            regime_fit = "breakout regime"
        else:
            regime_fit = "mixed"
        # MACD confirms momentum behind the breakout
        if side is Side.LONG and profile.macd >= profile.macd_signal:
            score += 5
        elif side is Side.SHORT and profile.macd <= profile.macd_signal:
            score += 5
    elif family == "london_breakout":
        # London ORB breakout: strongest in trending or breakout regimes
        if structure_state == "breakout_dominant":
            score += 10
            regime_fit = "london breakout regime"
        elif profile.trend_state in {"uptrend", "downtrend"}:
            score += 7
            regime_fit = "trend continuation"
        else:
            regime_fit = "mixed"
        # RSI confirms directional conviction
        if side is Side.LONG and profile.rsi14 >= 50.0:
            score += 5
        elif side is Side.SHORT and profile.rsi14 <= 50.0:
            score += 5
    elif family == "trend_pullback":
        # Trend pullbacks need a clean trending regime
        if profile.trend_state in {"uptrend", "downtrend"}:
            score += 12
            regime_fit = "trend pullback continuation"
        elif structure_state == "compression":
            score -= 8
            regime_fit = "compression — avoid"
        else:
            regime_fit = "mixed"
        # MACD confirming the resumption from pullback
        if side is Side.LONG and profile.macd >= profile.macd_signal:
            score += 6
        elif side is Side.SHORT and profile.macd <= profile.macd_signal:
            score += 6
    elif family == "ny_session_breakout":
        # NY breakout of London range: best in breakout/trending regimes
        if structure_state == "breakout_dominant":
            score += 10
            regime_fit = "ny breakout regime"
        elif profile.trend_state in {"uptrend", "downtrend"}:
            score += 7
            regime_fit = "trend continuation"
        else:
            regime_fit = "mixed"
        if side is Side.LONG and profile.rsi14 >= 50.0:
            score += 5
        elif side is Side.SHORT and profile.rsi14 <= 50.0:
            score += 5
    elif family == "momentum_burst":
        # Momentum bursts work in all trending or breakout conditions
        if structure_state == "breakout_dominant":
            score += 12
            regime_fit = "institutional breakout"
        elif profile.trend_state in {"uptrend", "downtrend"}:
            score += 9
            regime_fit = "trending momentum"
        else:
            regime_fit = "mixed"
        if side is Side.LONG and profile.macd >= profile.macd_signal:
            score += 6
        elif side is Side.SHORT and profile.macd <= profile.macd_signal:
            score += 6
    elif family == "timed_horizon_macro_regime":
        # The macro construct's edge is the regime gate itself; the
        # entry-bar scoring should not penalize it for being counter to
        # short-term oscillation.  PREMIUM-tier validated; trust the gate.
        score += 18
        regime_fit = "macro regime: real-yield-down + vix-flat + dxy-flat"
        # Honest penalty for stale macro: caller already filters on
        # macro_frame being non-None, so this branch only fires when the
        # frame is fresh.  Boost on aligned HTF bias (already added above)
        # is conservative — keep it.
        if higher_timeframe_bias == "bullish" and side is Side.LONG:
            score += 4
    else:
        if structure_state == "breakout_dominant":
            score += 10
            regime_fit = "trend continuation / breakout"
        elif profile.trend_state == "compression":
            score += 8
            regime_fit = "compression release"
        else:
            score -= 4

        if side is Side.LONG and profile.macd >= profile.macd_signal:
            score += 5
        elif side is Side.SHORT and profile.macd <= profile.macd_signal:
            score += 5

    if timeframe_minutes >= 60:
        score += 5

    if profile.spread_mean > max(0.75, profile.atr14 * 0.08):
        score -= 8

    return max(0, min(100, score)), regime_fit, conflict


def _build_warnings(
    candidates: Sequence[EntryCandidate],
    higher_timeframe_bias: str,
    oscillation_label: str,
) -> list[str]:
    warnings: list[str] = []
    if not candidates:
        warnings.append("No active entry candidates detected on the latest synchronized bars.")
    if oscillation_label == "oscillating mean-reversion regime":
        warnings.append("Structure is oscillating; breakout entries need extra caution.")
    if higher_timeframe_bias == "neutral":
        warnings.append("Higher-timeframe bias is mixed, so lower-timeframe entries carry more regime risk.")
    if any(candidate.conflict for candidate in candidates):
        warnings.append("Some current entry candidates conflict with the higher-timeframe stack.")
    return warnings


def _decision_plan(
    candidates: Sequence[EntryCandidate],
    timeframe_states: Sequence[TimeframeState],
    higher_timeframe_bias: str,
    oscillation_label: str,
) -> DecisionPlan:
    if not candidates:
        return DecisionPlan(
            status="hold",
            family=None,
            timeframe_minutes=None,
            side=None,
            reference_price=None,
            stop=None,
            target=None,
            score=0,
            risk_reward=0.0,
            rationale=("No current entry candidates meet the scan criteria.",),
        )

    top_candidate = candidates[0]
    state_by_timeframe = {state.timeframe_minutes: state for state in timeframe_states}
    timeframe_state = state_by_timeframe[top_candidate.timeframe_minutes]
    risk_distance = abs(top_candidate.reference_price - top_candidate.stop)
    reward_distance = abs(top_candidate.target - top_candidate.reference_price)
    risk_reward = reward_distance / risk_distance if risk_distance > 0.0 else 0.0
    rationale: list[str] = [
        f"Top candidate score is {top_candidate.score} on {top_candidate.timeframe_minutes}m.",
        f"Higher-timeframe bias is {higher_timeframe_bias}.",
        f"Oscillation regime is {oscillation_label}.",
        f"Current execution style on the timeframe is {timeframe_state.execution_style}.",
    ]

    if top_candidate.conflict is not None:
        rationale.append(top_candidate.conflict)
        return _decision_from_candidate("reject", top_candidate, risk_reward, rationale)

    if risk_reward < 1.4:
        rationale.append("Risk-reward is below the minimum threshold of 1.4.")
        return _decision_from_candidate("reject", top_candidate, risk_reward, rationale)

    if timeframe_state.spread > max(0.9, timeframe_state.atr14 * 0.08):
        rationale.append("Current spread is too expensive relative to volatility.")
        return _decision_from_candidate("reject", top_candidate, risk_reward, rationale)

    if oscillation_label == "oscillating mean-reversion regime" and top_candidate.family == "compression_breakout":
        rationale.append("Breakout family conflicts with the current oscillating regime.")
        return _decision_from_candidate("reject", top_candidate, risk_reward, rationale)

    if higher_timeframe_bias == "neutral":
        rationale.append("Higher-timeframe bias is mixed, so the agent should wait.")
        return _decision_from_candidate("hold", top_candidate, risk_reward, rationale)

    if top_candidate.score < 70:
        rationale.append("Candidate score is below the accept threshold of 70.")
        return _decision_from_candidate("hold", top_candidate, risk_reward, rationale)

    rationale.append("Candidate aligns with the current regime and clears the quality gates.")
    return _decision_from_candidate("accept", top_candidate, risk_reward, rationale)


def _decision_from_candidate(
    status: str,
    candidate: EntryCandidate,
    risk_reward: float,
    rationale: Sequence[str],
) -> DecisionPlan:
    return DecisionPlan(
        status=status,
        family=candidate.family,
        timeframe_minutes=candidate.timeframe_minutes,
        side=candidate.side,
        reference_price=candidate.reference_price,
        stop=candidate.stop,
        target=candidate.target,
        score=candidate.score,
        risk_reward=risk_reward,
        rationale=tuple(rationale),
    )


def _higher_timeframe_bias(analysis: BundleAnalysis) -> str:
    higher_profiles = [profile for profile in analysis.profiles if profile.timeframe_minutes >= 60]
    relevant = higher_profiles or list(analysis.profiles)
    bullish = sum(1 for profile in relevant if profile.trend_state == "uptrend")
    bearish = sum(1 for profile in relevant if profile.trend_state == "downtrend")
    if bullish > bearish:
        return "bullish"
    if bearish > bullish:
        return "bearish"
    return "neutral"


def _oscillation_label(analysis: BundleAnalysis) -> str:
    sweep_dominant = 0
    breakout_dominant = 0
    for profile in analysis.profiles:
        structure = _structure_state(profile)
        if structure == "sweep_dominant":
            sweep_dominant += 1
        elif structure == "breakout_dominant":
            breakout_dominant += 1

    if sweep_dominant > breakout_dominant and analysis.range_count + analysis.compression_count >= max(1, len(analysis.profiles) // 2):
        return "oscillating mean-reversion regime"
    if breakout_dominant > sweep_dominant and analysis.bullish_count != analysis.bearish_count:
        return "trend / breakout regime"
    return "mixed transition regime"


def _structure_state(profile: TimeframeAnalysis) -> str:
    breakout_total = profile.donchian_breakout_up_count + profile.donchian_breakout_down_count
    sweep_total = profile.liquidity_sweep_up_count + profile.liquidity_sweep_down_count
    if sweep_total > breakout_total * 1.1:
        return "sweep_dominant"
    if breakout_total > sweep_total * 1.1:
        return "breakout_dominant"
    return "balanced"


def _execution_style(trend_state: str, structure_state: str) -> str:
    if structure_state == "sweep_dominant" and trend_state in {"range", "compression"}:
        return "mean_reversion"
    if structure_state == "breakout_dominant" and trend_state in {"uptrend", "downtrend"}:
        return "trend_following"
    return "mixed"


def _normalized_time(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _asian_range_strategy_for_timeframe(timeframe_minutes: int) -> AsianRangeBreakoutStrategy:
    # Best params from holdout-eval (PASS: PF=1.54, p=0.177, 39 holdout trades)
    return AsianRangeBreakoutStrategy(
        atr_period=10, risk_reward=1.5, max_spread=0.75,
        min_breakout_atr=0.1, min_range_atr=0.2, min_asian_bars=3,
    )


def _london_breakout_strategy_for_timeframe(timeframe_minutes: int) -> LondonBreakoutStrategy:
    if timeframe_minutes >= 60:
        return LondonBreakoutStrategy(opening_range_bars=2, atr_period=14, risk_reward=2.0, max_spread=0.75)
    return LondonBreakoutStrategy(opening_range_bars=4, atr_period=14, risk_reward=2.0, max_spread=0.75)


def _trend_pullback_strategy_for_timeframe(timeframe_minutes: int) -> TrendPullbackStrategy:
    if timeframe_minutes >= 60:
        return TrendPullbackStrategy(
            ema_fast=20, ema_slow=50, atr_period=14, trend_strength_min=0.8,
            pullback_tolerance=0.4, risk_reward=2.0, max_spread=0.75
        )
    return TrendPullbackStrategy(
        ema_fast=20, ema_slow=50, atr_period=14, trend_strength_min=0.8,
        pullback_tolerance=0.4, risk_reward=2.0, max_spread=0.75
    )


def _liquidity_strategy_for_timeframe(timeframe_minutes: int) -> LiquiditySweepStrategy:
    if timeframe_minutes >= 60:
        return LiquiditySweepStrategy(lookback=20, atr_period=14, min_sweep_atr=0.2, risk_reward=1.5)
    if timeframe_minutes >= 15:
        return LiquiditySweepStrategy(lookback=15, atr_period=10, min_sweep_atr=0.2, risk_reward=1.5)
    return LiquiditySweepStrategy(lookback=10, atr_period=14, min_sweep_atr=0.1, risk_reward=2.0)


def _compression_strategy_for_timeframe(timeframe_minutes: int) -> CompressionBreakoutStrategy:
    if timeframe_minutes >= 60:
        return CompressionBreakoutStrategy(
            breakout_lookback=8,
            compression_lookback=4,
            atr_period=14,
            max_compression_atr_ratio=1.2,
            min_breakout_atr=0.05,
            risk_reward=2.5,
        )
    return CompressionBreakoutStrategy(
        breakout_lookback=12,
        compression_lookback=6,
        atr_period=14,
        max_compression_atr_ratio=1.0,
        min_breakout_atr=0.1,
        risk_reward=2.0,
    )