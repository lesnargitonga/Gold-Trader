"""MTF paper-signal runner — Phase 11 follow-up.

Standalone script that evaluates the validated MTF construct against
fresh bars and records signals to a side-channel paper journal.
Designed to run from cron alongside (NOT inside) the legacy
``agent-cycle`` pipeline, because the legacy pipeline is single-TF
and an MTF graft would require a real refactor.

Construct (Phase 11 verdict, do not change without re-running
tests/test_mtf_edge_regression.py):

    HTFBreakoutContinuation(align_tf="240m",
                            range_lookback=18,
                            risk_reward=2.5)
    wrapped in
    RegimeGatedMTF(align_tf="240m",
                   min_trend_strength_atr=0.5,
                   atr_pct_window=100,
                   atr_pct_low=0.20,
                   atr_pct_high=0.90)

Inputs (read-only):
    DATA_DIR/<symbol>_60m.csv     primary
    DATA_DIR/<symbol>_240m.csv    HTF context

Outputs (idempotent appends):
    JOURNAL: logs/mtf_paper_journal.csv
        timestamp_run, signal_bar_ts, side, entry, stop, target, rr
    LATEST:  <DATA_DIR>/mtf_latest_signal.json
        {bar_ts, side, entry, stop, target, rr, generated_at}
        (overwritten each run; consumed by web UI / monitoring)

Usage:
    python scripts/mtf_paper_signal.py \\
        --data-dir data/agent_live_xauusd \\
        --symbol xauusd

The script does NOT place orders.  It is paper-only by construction,
producing a deterministic decision log against which we accumulate
the ≥80 forward trades required before any live capital commitment.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from gold_trader.backtest import build_indicator_caches, run_mtf_backtest
from gold_trader.data import build_mtf_bundle, load_bars_from_csv
from gold_trader.models import BacktestConfig
from gold_trader.strategies.mtf_strategies import (
    HTFBreakoutContinuation,
    RegimeGatedMTF,
)


CONFIG = BacktestConfig(
    starting_equity=100_000.0,
    risk_fraction=0.01,
    max_hold_bars=24,
    kill_switch_drawdown_fraction=None,
    slippage_bps=2.0,
    commission_per_trade=1.0,
)


def make_strategy() -> RegimeGatedMTF:
    inner = HTFBreakoutContinuation(
        align_tf="240m", range_lookback=18, risk_reward=2.5,
    )
    return RegimeGatedMTF(
        inner=inner, align_tf="240m",
        min_trend_strength_atr=0.5,
        atr_pct_window=100,
        atr_pct_low=0.20, atr_pct_high=0.90,
    )


def _journal_seen(journal_path: Path, bar_ts_iso: str) -> bool:
    if not journal_path.exists():
        return False
    with journal_path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("signal_bar_ts") == bar_ts_iso:
                return True
    return False


def _journal_append(journal_path: Path, row: dict) -> None:
    fields = ["timestamp_run", "signal_bar_ts", "side",
              "entry", "stop", "target", "rr"]
    new_file = not journal_path.exists()
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", required=True,
                   help="directory containing <symbol>_60m.csv and <symbol>_240m.csv")
    p.add_argument("--symbol", default="xauusd")
    p.add_argument("--journal", default=str(REPO_ROOT / "logs" / "mtf_paper_journal.csv"))
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    sym = args.symbol.lower()
    primary_csv = data_dir / f"{sym}_60m.csv"
    htf_csv = data_dir / f"{sym}_240m.csv"
    journal_path = Path(args.journal)
    latest_path = data_dir / "mtf_latest_signal.json"
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not primary_csv.exists() or not htf_csv.exists():
        print(f"mtf_paper_signal: missing input csvs at {data_dir} "
              f"(need {primary_csv.name} + {htf_csv.name})")
        return 0  # not an error — agent-cycle hasn't synced yet

    primary = load_bars_from_csv(primary_csv)
    htf240 = load_bars_from_csv(htf_csv)

    if len(primary) < 100 or len(htf240) < 50:
        print(f"mtf_paper_signal: insufficient bars "
              f"(primary={len(primary)}, htf={len(htf240)})")
        return 0

    bundle = build_mtf_bundle("60m", primary, {"240m": htf240})
    indicators = build_indicator_caches(bundle)
    strategy = make_strategy()
    res = run_mtf_backtest(bundle, strategy, CONFIG, indicators=indicators)

    last_bar_ts = primary[-1].timestamp.isoformat()
    if not res.trades:
        latest = {
            "bar_ts": last_bar_ts,
            "side": None,
            "generated_at": now_iso,
            "n_trades_in_window": 0,
            "note": "no signal in current data window",
        }
        latest_path.write_text(json.dumps(latest, indent=2))
        print(f"mtf_paper_signal: no signal at last bar {last_bar_ts}")
        return 0

    last_trade = res.trades[-1]
    signal_bar_ts = last_trade.entry_time.isoformat()
    rr = (last_trade.target - last_trade.entry_price) / max(
        abs(last_trade.entry_price - last_trade.stop), 1e-9
    )

    latest = {
        "bar_ts": last_bar_ts,
        "signal_bar_ts": signal_bar_ts,
        "side": last_trade.side.name,
        "entry": last_trade.entry_price,
        "stop": last_trade.stop,
        "target": last_trade.target,
        "rr": abs(rr),
        "generated_at": now_iso,
        "n_trades_in_window": len(res.trades),
    }
    latest_path.write_text(json.dumps(latest, indent=2))

    if _journal_seen(journal_path, signal_bar_ts):
        print(f"mtf_paper_signal: latest signal {signal_bar_ts} "
              f"already journaled (no-op)")
        return 0

    _journal_append(journal_path, {
        "timestamp_run": now_iso,
        "signal_bar_ts": signal_bar_ts,
        "side": last_trade.side.name,
        "entry": f"{last_trade.entry_price:.5f}",
        "stop": f"{last_trade.stop:.5f}",
        "target": f"{last_trade.target:.5f}",
        "rr": f"{abs(rr):.3f}",
    })
    print(f"mtf_paper_signal: APPENDED {last_trade.side.name} @ "
          f"{last_trade.entry_price:.2f} sl={last_trade.stop:.2f} "
          f"tp={last_trade.target:.2f} bar={signal_bar_ts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
