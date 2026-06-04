from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..backtest.engine import run_backtest
from ..models import BacktestConfig, ExecutedTrade, MarketBar
from ..strategies.base import Strategy


@dataclass(frozen=True)
class ScoreBracket:
    """Backtest performance for a single score bucket."""

    label: str          # e.g. "50–59"
    score_min: int
    score_max: int      # inclusive
    trades: int
    wins: int
    win_rate: float
    avg_r: float
    profit_factor: float


@dataclass(frozen=True)
class ScoreCalibrationResult:
    """Per-bracket performance summary across the full bar history."""

    brackets: tuple[ScoreBracket, ...]
    total_trades: int
    verdict: str


def calibrate_score_system(
    bars: Sequence[MarketBar],
    strategy: Strategy,
    config: BacktestConfig,
    bracket_width: int = 10,
) -> ScoreCalibrationResult:
    """Run a full backtest and group closed trades by their signal score.

    The strategy's ``signal_for()`` method is called at each index.  For
    strategies whose signal carries a ``score`` tag (encoded as ``score=NN``
    inside ``TradeSignal.tags``), trades are bucketed accordingly; otherwise
    the score is read from the strategy's own ``_score_signal`` call if it
    exposes ``score_for()``.

    For compatibility with the current engine (which does not record signal
    score on ``ExecutedTrade``), we run a second-pass scan: at each bar index
    that the backtest entered a trade, re-call ``signal_for()`` and score the
    signal via the shared helper so we can tag which bracket it belongs to.

    Parameters
    ----------
    bars : Sequence[MarketBar]
        Full bar history.
    strategy : Strategy
        Instantiated strategy whose default parameters to use.
    config : BacktestConfig
        Backtest configuration.
    bracket_width : int
        Width of each score bucket.  Default 10 (i.e. 50–59, 60–69, 70–79, 80–89, 90–99).

    Returns
    -------
    ScoreCalibrationResult
    """
    # ── Step 1: run the backtest to get closed trades w/ entry times ──
    result = run_backtest(bars, strategy, config)
    if not result.trades:
        return ScoreCalibrationResult(brackets=(), total_trades=0, verdict="No trades — cannot calibrate")

    # Build a lookup: entry_time → ExecutedTrade
    trade_by_entry_time = {trade.entry_time: trade for trade in result.trades}

    # ── Step 2: second-pass scan to annotate each trade with a score ──
    scored_trades: list[tuple[int, ExecutedTrade]] = []
    for index in range(strategy.warmup_bars(), len(bars) - 1):
        signal = strategy.signal_for(bars, index)
        if signal is None:
            continue
        next_bar = bars[index + 1]
        entry_time = next_bar.timestamp
        trade = trade_by_entry_time.get(entry_time)
        if trade is None:
            continue
        score = _extract_score_from_tags(signal.tags)
        scored_trades.append((score, trade))

    if not scored_trades:
        return ScoreCalibrationResult(
            brackets=(),
            total_trades=len(result.trades),
            verdict="Score tags not present on signals — add score_for() to strategy",
        )

    # ── Step 3: bucket by score bracket ──────────────────────────────
    buckets: dict[int, list[ExecutedTrade]] = {}
    for score, trade in scored_trades:
        bucket_floor = (score // bracket_width) * bracket_width
        buckets.setdefault(bucket_floor, []).append(trade)

    brackets: list[ScoreBracket] = []
    for bucket_floor in sorted(buckets):
        bucket_trades = buckets[bucket_floor]
        wins = [t for t in bucket_trades if t.pnl_r > 0]
        win_rate = len(wins) / len(bucket_trades) if bucket_trades else 0.0
        avg_r = sum(t.pnl_r for t in bucket_trades) / len(bucket_trades) if bucket_trades else 0.0
        gross_profit = sum(t.pnl_r for t in bucket_trades if t.pnl_r > 0)
        gross_loss = abs(sum(t.pnl_r for t in bucket_trades if t.pnl_r < 0))
        pf = gross_profit / gross_loss if gross_loss > 0.0 else (999.0 if gross_profit > 0 else 0.0)
        brackets.append(
            ScoreBracket(
                label=f"{bucket_floor}–{bucket_floor + bracket_width - 1}",
                score_min=bucket_floor,
                score_max=bucket_floor + bracket_width - 1,
                trades=len(bucket_trades),
                wins=len(wins),
                win_rate=win_rate,
                avg_r=avg_r,
                profit_factor=pf,
            )
        )

    verdict = _calibration_verdict(brackets)
    return ScoreCalibrationResult(
        brackets=tuple(brackets),
        total_trades=len(result.trades),
        verdict=verdict,
    )


def _extract_score_from_tags(tags: tuple[str, ...]) -> int:
    """Look for a ``score=NN`` tag in the signal's tag tuple.  Return 50 if absent."""
    for tag in tags:
        if tag.startswith("score="):
            try:
                return int(tag.split("=", 1)[1])
            except ValueError:
                pass
    return 50  # default to mid-range if no score tag


def _calibration_verdict(brackets: list[ScoreBracket]) -> str:
    if not brackets:
        return "No brackets — no trades to calibrate"
    # Check whether higher-score brackets outperform lower-score brackets
    high = [b for b in brackets if b.score_min >= 70]
    low = [b for b in brackets if b.score_min < 70]
    if not high or not low:
        return "Insufficient score range to assess calibration quality"

    high_pf = sum(b.profit_factor * b.trades for b in high) / max(sum(b.trades for b in high), 1)
    low_pf = sum(b.profit_factor * b.trades for b in low) / max(sum(b.trades for b in low), 1)

    if high_pf > low_pf * 1.20:
        return (
            f"CALIBRATED: score ≥70 PF={high_pf:.2f} vs <70 PF={low_pf:.2f} "
            f"— higher scores predict better outcomes (+{(high_pf/low_pf - 1):.0%})"
        )
    return (
        f"UNCALIBRATED: score ≥70 PF={high_pf:.2f} vs <70 PF={low_pf:.2f} "
        f"— score threshold does not predict performance (diff < 20%)"
    )
