from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Sequence

from ..models import MarketBar


@dataclass(frozen=True)
class TimeframeAnalysis:
    timeframe_minutes: int
    bar_count: int
    start_time: datetime
    end_time: datetime
    total_return: float
    return_volatility: float
    atr14: float
    ema_fast: float
    ema_slow: float
    rsi14: float
    macd: float
    macd_signal: float
    bollinger_width: float
    compression_ratio: float
    spread_mean: float
    spread_max: float
    positive_bar_ratio: float
    directional_persistence: float
    trend_state: str
    trend_strength: float
    donchian_breakout_up_count: int
    donchian_breakout_down_count: int
    liquidity_sweep_up_count: int
    liquidity_sweep_down_count: int
    compression_count: int
    best_session: str
    worst_session: str


@dataclass(frozen=True)
class BundleAnalysis:
    profiles: tuple[TimeframeAnalysis, ...]
    alignment_label: str
    bullish_count: int
    bearish_count: int
    compression_count: int
    range_count: int


def analyze_timeframe_bundle(datasets: dict[int, Sequence[MarketBar]]) -> BundleAnalysis:
    profiles = tuple(
        analyze_timeframe(timeframe_minutes, datasets[timeframe_minutes])
        for timeframe_minutes in sorted(datasets)
    )
    bullish_count = sum(1 for profile in profiles if profile.trend_state == "uptrend")
    bearish_count = sum(1 for profile in profiles if profile.trend_state == "downtrend")
    compression_count = sum(1 for profile in profiles if profile.trend_state == "compression")
    range_count = sum(1 for profile in profiles if profile.trend_state == "range")

    return BundleAnalysis(
        profiles=profiles,
        alignment_label=_alignment_label(bullish_count, bearish_count, compression_count, len(profiles)),
        bullish_count=bullish_count,
        bearish_count=bearish_count,
        compression_count=compression_count,
        range_count=range_count,
    )


def analyze_timeframe(timeframe_minutes: int, bars: Sequence[MarketBar]) -> TimeframeAnalysis:
    if not bars:
        raise ValueError("bars must not be empty")

    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    spreads = [bar.spread for bar in bars]
    returns = _returns(closes)
    positive_bar_ratio = sum(1 for value in returns if value > 0.0) / len(returns) if returns else 0.0
    directional_persistence = _directional_persistence(returns)

    atr_series = _atr_series(bars, 14)
    ema_fast_series = _ema(closes, 20)
    ema_slow_series = _ema(closes, 50)
    rsi_series = _rsi(closes, 14)
    macd_line, macd_signal = _macd(closes)
    bollinger_width_series = _bollinger_width(closes, 20)
    compression_ratio_series = _compression_ratio_series(bars, atr_series, 10)

    latest_atr = _last_value(atr_series)
    latest_ema_fast = _last_value(ema_fast_series)
    latest_ema_slow = _last_value(ema_slow_series)
    latest_rsi = _last_value(rsi_series)
    latest_macd = _last_value(macd_line)
    latest_macd_signal = _last_value(macd_signal)
    latest_bollinger_width = _last_value(bollinger_width_series)
    latest_compression_ratio = _last_value(compression_ratio_series)

    trend_strength = 0.0
    if latest_atr > 0.0:
        trend_strength = (latest_ema_fast - latest_ema_slow) / latest_atr

    return TimeframeAnalysis(
        timeframe_minutes=timeframe_minutes,
        bar_count=len(bars),
        start_time=bars[0].timestamp,
        end_time=bars[-1].timestamp,
        total_return=((closes[-1] - closes[0]) / closes[0]) if closes[0] else 0.0,
        return_volatility=pstdev(returns) if len(returns) > 1 else 0.0,
        atr14=latest_atr,
        ema_fast=latest_ema_fast,
        ema_slow=latest_ema_slow,
        rsi14=latest_rsi,
        macd=latest_macd,
        macd_signal=latest_macd_signal,
        bollinger_width=latest_bollinger_width,
        compression_ratio=latest_compression_ratio,
        spread_mean=mean(spreads),
        spread_max=max(spreads),
        positive_bar_ratio=positive_bar_ratio,
        directional_persistence=directional_persistence,
        trend_state=_trend_state(trend_strength, latest_compression_ratio),
        trend_strength=trend_strength,
        donchian_breakout_up_count=_count_donchian_breakouts(highs, lows, closes, 20, "up"),
        donchian_breakout_down_count=_count_donchian_breakouts(highs, lows, closes, 20, "down"),
        liquidity_sweep_up_count=_count_liquidity_sweeps(highs, lows, closes, 20, "up"),
        liquidity_sweep_down_count=_count_liquidity_sweeps(highs, lows, closes, 20, "down"),
        compression_count=_count_compression_bars(compression_ratio_series, 0.8),
        best_session=_session_rank(bars, best=True),
        worst_session=_session_rank(bars, best=False),
    )


def write_bundle_analysis_report(
    datasets: dict[int, Sequence[MarketBar]],
    analysis: BundleAnalysis,
    output_dir: str | Path,
    include_charts: bool = True,
) -> Path:
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    charts_dir = target_dir / "charts"
    if include_charts:
        charts_dir.mkdir(parents=True, exist_ok=True)
        _plot_bundle_overview(analysis, charts_dir / "overview.png")
        for timeframe_minutes, bars in sorted(datasets.items()):
            _plot_timeframe_chart(
                timeframe_minutes=timeframe_minutes,
                bars=bars,
                output_path=charts_dir / f"timeframe_{timeframe_minutes}m.png",
            )

    report_path = target_dir / "report.md"
    report_path.write_text(
        _render_report_markdown(analysis, include_charts=include_charts),
        encoding="utf-8",
    )
    return report_path


def _render_report_markdown(analysis: BundleAnalysis, include_charts: bool) -> str:
    lines = ["# Multi-Timeframe Market Analysis", ""]
    lines.append(f"Alignment: **{analysis.alignment_label}**")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Timeframe | Bars | Return | Trend | RSI14 | ATR14 | Spread Mean | Breakouts U/D | Sweeps U/D | Compression | Best Session | Worst Session |")
    lines.append("| --- | ---: | ---: | --- | ---: | ---: | ---: | --- | --- | ---: | --- | --- |")

    for profile in analysis.profiles:
        lines.append(
            "| "
            f"{profile.timeframe_minutes}m | {profile.bar_count} | {profile.total_return:.2%} | {profile.trend_state} | "
            f"{profile.rsi14:.1f} | {profile.atr14:.3f} | {profile.spread_mean:.3f} | "
            f"{profile.donchian_breakout_up_count}/{profile.donchian_breakout_down_count} | "
            f"{profile.liquidity_sweep_up_count}/{profile.liquidity_sweep_down_count} | "
            f"{profile.compression_count} | {profile.best_session} | {profile.worst_session} |"
        )

    lines.append("")
    lines.append("## Observations")
    lines.append("")
    for profile in analysis.profiles:
        lines.append(
            "- "
            + _profile_observation(profile)
        )

    if include_charts:
        lines.append("")
        lines.append("## Charts")
        lines.append("")
        lines.append("- Overview: [charts/overview.png](charts/overview.png)")
        for profile in analysis.profiles:
            lines.append(
                f"- {profile.timeframe_minutes}m chart: [charts/timeframe_{profile.timeframe_minutes}m.png](charts/timeframe_{profile.timeframe_minutes}m.png)"
            )

    lines.append("")
    lines.append("## Interpretation Rules")
    lines.append("")
    lines.append("- `uptrend` or `downtrend` is based on the EMA20/EMA50 gap normalized by ATR14.")
    lines.append("- `compression` flags timeframes where recent range is small relative to ATR.")
    lines.append("- Breakout and sweep counts are descriptive structure frequencies, not direct trade signals.")
    lines.append("- The report describes market regimes; it does not prove alpha by itself.")
    lines.append("")
    return "\n".join(lines)


def _profile_observation(profile: TimeframeAnalysis) -> str:
    breakout_bias = "breakout-heavy" if profile.donchian_breakout_up_count + profile.donchian_breakout_down_count > profile.liquidity_sweep_up_count + profile.liquidity_sweep_down_count else "sweep-heavy"
    friction = "high-friction" if profile.spread_mean > max(profile.atr14 * 0.25, 0.5) else "normal-friction"
    momentum = "overbought" if profile.rsi14 >= 70 else "oversold" if profile.rsi14 <= 30 else "balanced"
    return (
        f"{profile.timeframe_minutes}m is {profile.trend_state} with {momentum} momentum, {breakout_bias} structure frequency, "
        f"{friction} execution conditions, and session edge leaning toward {profile.best_session} over {profile.worst_session}."
    )


def _plot_bundle_overview(analysis: BundleAnalysis, output_path: Path) -> None:
    plt = _pyplot()
    timeframes = [f"{profile.timeframe_minutes}m" for profile in analysis.profiles]
    trend_strengths = [profile.trend_strength for profile in analysis.profiles]
    returns = [profile.total_return * 100 for profile in analysis.profiles]
    breakout_minus_sweeps = [
        (profile.donchian_breakout_up_count + profile.donchian_breakout_down_count)
        - (profile.liquidity_sweep_up_count + profile.liquidity_sweep_down_count)
        for profile in analysis.profiles
    ]

    figure, axes = plt.subplots(3, 1, figsize=(12, 10), constrained_layout=True)
    axes[0].bar(timeframes, trend_strengths, color="#2f6f4f")
    axes[0].set_title("Trend Strength by Timeframe")
    axes[0].axhline(0.0, color="#333333", linewidth=1)
    axes[0].set_ylabel("EMA gap / ATR")

    axes[1].bar(timeframes, returns, color="#9c4f24")
    axes[1].set_title("Total Return over Dataset")
    axes[1].set_ylabel("Percent")

    axes[2].bar(timeframes, breakout_minus_sweeps, color="#2c4b8e")
    axes[2].set_title("Breakout Count Minus Sweep Count")
    axes[2].set_ylabel("Count")

    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def _plot_timeframe_chart(timeframe_minutes: int, bars: Sequence[MarketBar], output_path: Path) -> None:
    plt = _pyplot()
    closes = [bar.close for bar in bars]
    spreads = [bar.spread for bar in bars]
    timestamps = [bar.timestamp for bar in bars]
    ema_fast = _ema(closes, 20)
    ema_slow = _ema(closes, 50)
    rsi_series = _rsi(closes, 14)
    atr_series = _atr_series(bars, 14)
    macd_line, macd_signal = _macd(closes)

    figure, axes = plt.subplots(4, 1, figsize=(14, 12), sharex=True, constrained_layout=True)
    axes[0].plot(timestamps, closes, label="close", color="#1f1f1f", linewidth=1)
    axes[0].plot(timestamps, ema_fast, label="ema20", color="#2f6f4f", linewidth=1)
    axes[0].plot(timestamps, ema_slow, label="ema50", color="#9c4f24", linewidth=1)
    axes[0].set_title(f"{timeframe_minutes}m Price and Trend Structure")
    axes[0].legend(loc="upper left")

    axes[1].plot(timestamps, rsi_series, color="#2c4b8e", linewidth=1)
    axes[1].axhline(70, color="#666666", linestyle="--", linewidth=0.8)
    axes[1].axhline(30, color="#666666", linestyle="--", linewidth=0.8)
    axes[1].set_title("RSI14")

    axes[2].plot(timestamps, macd_line, label="macd", color="#7a3b69", linewidth=1)
    axes[2].plot(timestamps, macd_signal, label="signal", color="#3d7a89", linewidth=1)
    axes[2].axhline(0.0, color="#333333", linewidth=0.8)
    axes[2].set_title("MACD")
    axes[2].legend(loc="upper left")

    axes[3].plot(timestamps, atr_series, label="atr14", color="#9c4f24", linewidth=1)
    axes[3].plot(timestamps, spreads, label="spread", color="#444444", linewidth=1)
    axes[3].set_title("ATR14 and Spread")
    axes[3].legend(loc="upper left")

    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def _pyplot():
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    return plt


def _returns(closes: Sequence[float]) -> list[float]:
    return [
        (closes[index] - closes[index - 1]) / closes[index - 1]
        for index in range(1, len(closes))
        if closes[index - 1] != 0.0
    ]


def _ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    ema_values = [values[0]]
    for value in values[1:]:
        ema_values.append((value * alpha) + (ema_values[-1] * (1.0 - alpha)))
    return ema_values


def _sma(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    averages: list[float | None] = []
    running_sum = 0.0
    for index, value in enumerate(values):
        running_sum += value
        if index >= period:
            running_sum -= values[index - period]
        if index + 1 >= period:
            averages.append(running_sum / period)
        else:
            averages.append(None)
    return averages


def _rsi(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    rsi_values = [50.0]
    gains: list[float] = []
    losses: list[float] = []
    average_gain = 0.0
    average_loss = 0.0

    for index in range(1, len(values)):
        delta = values[index] - values[index - 1]
        gains.append(max(delta, 0.0))
        losses.append(abs(min(delta, 0.0)))

        if index < period:
            rsi_values.append(50.0)
            continue
        if index == period:
            average_gain = sum(gains) / period
            average_loss = sum(losses) / period
        else:
            average_gain = ((average_gain * (period - 1)) + gains[-1]) / period
            average_loss = ((average_loss * (period - 1)) + losses[-1]) / period

        if average_loss == 0.0:
            rsi_values.append(100.0)
        else:
            rs = average_gain / average_loss
            rsi_values.append(100.0 - (100.0 / (1.0 + rs)))

    return rsi_values


def _atr_series(bars: Sequence[MarketBar], period: int) -> list[float]:
    true_ranges: list[float] = []
    previous_close = None
    for bar in bars:
        true_ranges.append(bar.true_range(previous_close))
        previous_close = bar.close

    atr_values: list[float] = []
    for index in range(len(true_ranges)):
        start = max(0, index - period + 1)
        window = true_ranges[start:index + 1]
        atr_values.append(sum(window) / len(window))
    return atr_values


def _macd(values: Sequence[float]) -> tuple[list[float], list[float]]:
    ema12 = _ema(values, 12)
    ema26 = _ema(values, 26)
    macd_line = [fast - slow for fast, slow in zip(ema12, ema26)]
    signal_line = _ema(macd_line, 9)
    return macd_line, signal_line


def _bollinger_width(values: Sequence[float], period: int) -> list[float]:
    averages = _sma(values, period)
    widths: list[float] = []
    for index, average in enumerate(averages):
        if average is None or average == 0.0:
            widths.append(0.0)
            continue
        window = values[index - period + 1:index + 1]
        if len(window) < period:
            widths.append(0.0)
            continue
        deviation = pstdev(window)
        upper = average + (2.0 * deviation)
        lower = average - (2.0 * deviation)
        widths.append((upper - lower) / average)
    return widths


def _compression_ratio_series(
    bars: Sequence[MarketBar],
    atr_series: Sequence[float],
    lookback: int,
) -> list[float]:
    ratios: list[float] = []
    for index in range(len(bars)):
        start = max(0, index - lookback + 1)
        window = bars[start:index + 1]
        window_high = max(bar.high for bar in window)
        window_low = min(bar.low for bar in window)
        atr = atr_series[index] if index < len(atr_series) else 0.0
        ratios.append((window_high - window_low) / atr if atr > 0.0 else 0.0)
    return ratios


def _count_donchian_breakouts(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    lookback: int,
    direction: str,
) -> int:
    count = 0
    for index in range(lookback, len(closes)):
        prior_high = max(highs[index - lookback:index])
        prior_low = min(lows[index - lookback:index])
        if direction == "up" and closes[index] > prior_high:
            count += 1
        if direction == "down" and closes[index] < prior_low:
            count += 1
    return count


def _count_liquidity_sweeps(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    lookback: int,
    direction: str,
) -> int:
    count = 0
    for index in range(lookback, len(closes)):
        prior_high = max(highs[index - lookback:index])
        prior_low = min(lows[index - lookback:index])
        if direction == "up" and highs[index] > prior_high and closes[index] < prior_high:
            count += 1
        if direction == "down" and lows[index] < prior_low and closes[index] > prior_low:
            count += 1
    return count


def _count_compression_bars(compression_ratio_series: Sequence[float], threshold: float) -> int:
    return sum(1 for value in compression_ratio_series if value > 0.0 and value <= threshold)


def _directional_persistence(returns: Sequence[float]) -> float:
    signs = [1 if value > 0 else -1 for value in returns if value != 0.0]
    if len(signs) < 2:
        return 0.0
    matches = sum(1 for index in range(1, len(signs)) if signs[index] == signs[index - 1])
    return matches / (len(signs) - 1)


def _trend_state(trend_strength: float, compression_ratio: float) -> str:
    if compression_ratio > 0.0 and compression_ratio <= 0.8:
        return "compression"
    if trend_strength >= 0.5:
        return "uptrend"
    if trend_strength <= -0.5:
        return "downtrend"
    return "range"


def _session_rank(bars: Sequence[MarketBar], best: bool) -> str:
    session_returns: dict[str, list[float]] = {}
    for bar in bars:
        if bar.open == 0.0:
            continue
        session_returns.setdefault(bar.session, []).append((bar.close - bar.open) / bar.open)
    if not session_returns:
        return "unknown"
    key_fn = max if best else min
    ranked = key_fn(
        session_returns.items(),
        key=lambda item: mean(item[1]) if item[1] else 0.0,
    )
    return ranked[0]


def _last_value(values: Sequence[float]) -> float:
    return values[-1] if values else 0.0


def _alignment_label(
    bullish_count: int,
    bearish_count: int,
    compression_count: int,
    timeframe_count: int,
) -> str:
    if timeframe_count == 0:
        return "no-data"
    if bullish_count == timeframe_count:
        return "fully aligned bullish"
    if bearish_count == timeframe_count:
        return "fully aligned bearish"
    if compression_count >= max(1, timeframe_count // 2):
        return "compression-heavy mixed stack"
    if bullish_count > bearish_count:
        return "mixed bullish bias"
    if bearish_count > bullish_count:
        return "mixed bearish bias"
    return "balanced mixed stack"