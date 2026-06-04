"""Empirical replay of MacroDecisionFilter on ARB historical trades.

Question: would applying MacroDecisionFilter (verdict='block' rejected,
'allow'/'allow_with_warning' accepted) have improved ARB on the
15-month dataset?

Output: PF / win-rate / total-R for {all, allow-only, allow-and-warn,
strict-allow-only}, plus how many trades each policy keeps.

This is the operational test of "macro as filter, not signal" — the
direct response to the honest-eval feedback.  If filter improves PF
on this dataset *and* on a held-out window, we promote it from
GOLD_MACRO_FILTER=soft to hard.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.backtest.engine import run_backtest  # noqa: E402
from gold_trader.data import load_bars_from_csv  # noqa: E402
from gold_trader.data.macro import load_macro_frame  # noqa: E402
from gold_trader.macro_filter import MacroDecisionFilter  # noqa: E402
from gold_trader.models import BacktestConfig, Side  # noqa: E402
from gold_trader.strategies.asian_range_breakout import AsianRangeBreakoutStrategy  # noqa: E402


def _stats(trades: list) -> dict:
    if not trades:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "avg_r": 0.0, "total_r": 0.0}
    wins = [t for t in trades if t.pnl_r > 0]
    losses = [t for t in trades if t.pnl_r <= 0]
    gross_win = sum(t.pnl_r for t in wins)
    gross_loss = -sum(t.pnl_r for t in losses)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    return {
        "n": len(trades),
        "wr": len(wins) / len(trades),
        "pf": pf,
        "avg_r": sum(t.pnl_r for t in trades) / len(trades),
        "total_r": sum(t.pnl_r for t in trades),
    }


def _row(label: str, s: dict, base_n: int) -> None:
    pf = f"{s['pf']:>6.3f}" if s["pf"] != float("inf") else "    inf"
    kept = f"{100 * s['n'] / base_n:>5.1f}%" if base_n else "  -- "
    print(
        f"  {label:<32} n={s['n']:>4d} ({kept})  wr={s['wr']:>5.1%}  "
        f"pf={pf}  avg_r={s['avg_r']:>+6.3f}  total_r={s['total_r']:>+7.2f}"
    )


def main() -> int:
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "data/xauusd_full_15m.csv"
    macro_dir = Path("data/macro")

    bars = load_bars_from_csv(csv_path)
    print(f"bars: {len(bars)} ({bars[0].timestamp} -> {bars[-1].timestamp})")
    macro = load_macro_frame(macro_dir)
    print(f"macro frame: {sorted(macro.names())}")

    strat = AsianRangeBreakoutStrategy(
        atr_period=10,
        risk_reward=2.5,
        max_spread=1.00,
        min_atr_threshold=0.0,
    )
    config = BacktestConfig(
        starting_equity=10_000.0,
        commission_per_trade=10.0,
    )
    print(f"running ARB ({strat}) ...")
    result = run_backtest(bars, strat, config)
    trades = list(result.trades)
    base = _stats(trades)
    if not trades:
        print("no trades")
        return 0
    print()
    print("=== Baseline (no filter) ===")
    _row("ALL", base, base["n"])
    print()

    # Apply filter to each trade at entry time.
    fltr = MacroDecisionFilter(macro=macro)
    verdict_counts: Counter[str] = Counter()
    bucketed = {"allow": [], "allow_with_warning": [], "block": []}
    for t in trades:
        side = Side.LONG if str(t.side).lower().endswith("long") else Side.SHORT
        v = fltr.evaluate(side, t.entry_time)
        verdict_counts[v.verdict] += 1
        bucketed[v.verdict].append(t)

    print(f"verdict mix: {dict(verdict_counts)}")
    print()
    print("=== Filter policies (strategy=ARB on full dataset) ===")
    _row(
        "policy=allow_only",
        _stats(bucketed["allow"]),
        base["n"],
    )
    _row(
        "policy=allow+warn",
        _stats(bucketed["allow"] + bucketed["allow_with_warning"]),
        base["n"],
    )
    _row(
        "policy=block_only (rejected)",
        _stats(bucketed["block"]),
        base["n"],
    )

    print()
    print("Interpretation:")
    print(
        "  - If allow_only PF >> ALL PF AND keeps >=30%% of trades, the "
        "filter has empirical lift."
    )
    print(
        "  - If block_only PF < 1.0 with reasonable n, the filter is "
        "actually identifying losing regimes (good)."
    )
    print(
        "  - If allow+warn ~= ALL, the filter only matters at the strict "
        "boundary -> consider tightening."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
