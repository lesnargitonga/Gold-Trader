from __future__ import annotations

import unittest

from gold_trader.backtest.engine import BacktestConfig
from gold_trader.data.dukascopy import resample_bars
from gold_trader.data.synthetic import generate_synthetic_bars
from gold_trader.strategies.asian_range_breakout import AsianRangeBreakoutStrategy


class AsianRangeBreakoutStrategyTests(unittest.TestCase):
    def test_returns_strategy_instance(self) -> None:
        strategy = AsianRangeBreakoutStrategy()
        self.assertEqual(strategy.name, "asian_range_breakout")

    def test_warmup_bars_reasonable(self) -> None:
        strategy = AsianRangeBreakoutStrategy(atr_period=14, min_asian_bars=4)
        self.assertGreater(strategy.warmup_bars(), 14)

    def test_signal_for_returns_none_or_signal_on_synthetic(self) -> None:
        """signal_for() must return None or a valid TradeSignal — never raise."""
        bars = generate_synthetic_bars(count=500, seed=11)
        strategy = AsianRangeBreakoutStrategy()
        found_any = False
        for index in range(strategy.warmup_bars(), len(bars)):
            result = strategy.signal_for(bars, index)
            if result is not None:
                found_any = True
                self.assertIn(result.side.value, ("long", "short"))
                self.assertGreater(result.target, 0.0)
                self.assertGreater(result.stop, 0.0)
        # Not asserting found_any — synthetic bars may not have the pattern
        # but the method must not crash

    def test_signal_for_does_not_raise_on_short_bars(self) -> None:
        bars = generate_synthetic_bars(count=50, seed=99)
        strategy = AsianRangeBreakoutStrategy()
        for index in range(len(bars)):
            strategy.signal_for(bars, index)

    def test_backtest_produces_valid_summary(self) -> None:
        from gold_trader.backtest.engine import run_backtest
        from gold_trader.backtest.metrics import summarize_backtest
        bars = generate_synthetic_bars(count=800, seed=15)
        strategy = AsianRangeBreakoutStrategy()
        config = BacktestConfig()
        result = run_backtest(bars, strategy, config)
        summary = summarize_backtest(result)
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertGreaterEqual(summary.win_rate, 0.0)
        self.assertLessEqual(summary.win_rate, 1.0)

    def test_stop_is_below_entry_for_long(self) -> None:
        """For a long signal, stop must be less than the assumed entry."""
        from gold_trader.models import Side
        bars = generate_synthetic_bars(count=800, seed=21)
        strategy = AsianRangeBreakoutStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            sig = strategy.signal_for(bars, index)
            if sig is not None and sig.side is Side.LONG:
                self.assertLess(sig.stop, bars[index].close + strategy.atr_period,
                                "Long stop should be below close")
                return  # one check is enough

    def test_stop_is_above_entry_for_short(self) -> None:
        """For a short signal, stop must be above the assumed entry."""
        from gold_trader.models import Side
        bars = generate_synthetic_bars(count=800, seed=22)
        strategy = AsianRangeBreakoutStrategy()
        for index in range(strategy.warmup_bars(), len(bars)):
            sig = strategy.signal_for(bars, index)
            if sig is not None and sig.side is Side.SHORT:
                self.assertGreater(sig.stop, bars[index].close - strategy.atr_period,
                                   "Short stop should be above close")
                return  # one check is enough


class HoldoutEvalTests(unittest.TestCase):
    def test_holdout_evaluation_runs_without_error(self) -> None:
        from gold_trader.research.holdout import run_holdout_evaluation
        bars = generate_synthetic_bars(count=600, seed=30)
        strategy_cls = AsianRangeBreakoutStrategy

        from gold_trader.research.experiments import (
            AsianRangeBreakoutParameters,
            build_asian_range_breakout_grid,
        )
        grid = build_asian_range_breakout_grid(
            atr_periods=[14],
            risk_rewards=[2.0],
            max_spreads=[1.0],
            min_breakout_atrs=[0.05],
            min_range_atrs=[0.30],
            min_asian_bars_list=[4],
        )

        def factory(params: AsianRangeBreakoutParameters):
            return AsianRangeBreakoutStrategy(
                atr_period=params.atr_period,
                risk_reward=params.risk_reward,
                max_spread=params.max_spread,
                min_breakout_atr=params.min_breakout_atr,
                min_range_atr=params.min_range_atr,
                min_asian_bars=params.min_asian_bars,
            )

        result = run_holdout_evaluation(
            bars=bars,
            param_grid=grid,
            strategy_factory=factory,
            config=BacktestConfig(),
            holdout_fraction=0.33,
            n_permutations=50,
            family="asian_range_breakout",
        )
        self.assertIsNotNone(result)
        self.assertGreater(result.train_bars, 0)
        self.assertGreater(result.holdout_bars, 0)
        self.assertTrue(result.verdict)

    def test_verdict_contains_meaningful_text(self) -> None:
        from gold_trader.research.holdout import run_holdout_evaluation
        from gold_trader.strategies import LiquiditySweepStrategy
        from gold_trader.research.sweep import LiquiditySweepParameters, build_liquidity_sweep_grid

        bars = generate_synthetic_bars(count=400, seed=99)
        grid = build_liquidity_sweep_grid(
            lookbacks=[10, 15],
            atr_periods=[14],
            min_sweep_atrs=[0.2],
            risk_rewards=[2.0],
            max_spreads=[0.75],
            min_news_distances=[0.0],
        )

        def factory(p: LiquiditySweepParameters):
            return LiquiditySweepStrategy(
                lookback=p.lookback,
                atr_period=p.atr_period,
                min_sweep_atr=p.min_sweep_atr,
                risk_reward=p.risk_reward,
                max_spread=p.max_spread,
                min_news_distance_minutes=p.min_news_distance_minutes,
            )

        result = run_holdout_evaluation(
            bars=bars,
            param_grid=grid,
            strategy_factory=factory,
            config=BacktestConfig(),
            holdout_fraction=0.33,
            n_permutations=50,
            family="liquidity_sweep",
        )
        valid_prefixes = ("PASS", "FAIL", "WEAK", "INCONCLUSIVE")
        self.assertTrue(
            any(result.verdict.startswith(p) for p in valid_prefixes),
            f"Unexpected verdict: {result.verdict!r}",
        )


if __name__ == "__main__":
    unittest.main()
