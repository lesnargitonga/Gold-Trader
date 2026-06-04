from __future__ import annotations

from dataclasses import dataclass

from ..models import BacktestResult


@dataclass(frozen=True)
class BacktestSummary:
    total_trades: int
    win_rate: float
    average_r: float
    profit_factor: float
    max_drawdown: float
    total_pnl: float
    total_return: float
    ending_equity: float
    halted_by_kill_switch: bool


def summarize_backtest(result: BacktestResult) -> BacktestSummary:
    trades = list(result.trades)
    wins = [trade for trade in trades if trade.pnl > 0.0]
    losses = [trade for trade in trades if trade.pnl < 0.0]
    equity_curve = [result.starting_equity] + [trade.equity_after for trade in trades]

    total_pnl = result.ending_equity - result.starting_equity
    total_return = total_pnl / result.starting_equity if result.starting_equity else 0.0
    average_r = sum(trade.pnl_r for trade in trades) / len(trades) if trades else 0.0

    gross_profit = sum(trade.pnl for trade in wins)
    gross_loss = abs(sum(trade.pnl for trade in losses))
    if gross_loss == 0.0:
        profit_factor = float("inf") if gross_profit > 0.0 else 0.0
    else:
        profit_factor = gross_profit / gross_loss

    return BacktestSummary(
        total_trades=len(trades),
        win_rate=(len(wins) / len(trades)) if trades else 0.0,
        average_r=average_r,
        profit_factor=profit_factor,
        max_drawdown=_max_drawdown(equity_curve),
        total_pnl=total_pnl,
        total_return=total_return,
        ending_equity=result.ending_equity,
        halted_by_kill_switch=result.halted_by_kill_switch,
    )


def _max_drawdown(equity_curve: list[float]) -> float:
    peak = equity_curve[0]
    max_drawdown = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak == 0.0:
            continue
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return max_drawdown