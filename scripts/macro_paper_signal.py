"""Macro paper-signal runner — Phase 14b survivor.

Standalone script that evaluates the validated macro construct against
fresh bars and records signals to a side-channel paper journal.
Runs from cron alongside the existing pipeline (single-TF agent-cycle
and the MTF paper script).

Construct (Phase 14b PREMIUM verdict; see /memories/repo/notes.md):

    TimedHorizonMacroRegimeStrategy(
        real_yield_lookback_days=10,
        real_yield_max_change_bps=0.0,
        vix_lookback_days=5,
        vix_max_change_abs=2.5,
        require_dxy_flat=True,
        dxy_lookback_days=20,
        dxy_max_abs_change_pct=1.0,
        far_atr_mult=8.0,
        once_per_day=True,
    )

Survives:
    * retail costs ($1/trade + 2 bps slippage)
    * Bonferroni × 48-cell param search (effective p ~ 0)
    * 8/8 covered quarters positive (PF 1.28 -> 34.0)
    * cross-TF replication on 60m AND 240m (15m correctly fails)

Hard caveats:
    * Macro cache only covers 2024-05 -> 2026-05 (single bullish-gold
      regime epoch). Cannot prove "stays correctly silent" in chop.
    * Long-only by design.
    * Sparse: ~30 trades/yr on 60m, ~20/yr on 240m.

Inputs (read-only):
    DATA_DIR/<symbol>_60m.csv     primary bars
    MACRO_CACHE_DIR (data/macro)  real10y, dxy, vix CSVs

Outputs (idempotent appends):
    JOURNAL: logs/macro_paper_journal.csv
        timestamp_run, signal_bar_ts, side, entry, stop, target, rr, tf
    LATEST:  <DATA_DIR>/macro_latest_signal.json

Usage:
    python scripts/macro_paper_signal.py \
        --data-dir data/agent_live_xauusd \
        --symbol xauusd --tf 60m
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

from gold_trader.backtest import run_backtest
from gold_trader.data.csv_loader import load_bars_from_csv
from gold_trader.data.macro import load_macro_frame
from gold_trader.models import BacktestConfig
from gold_trader.strategies.timed_horizon_macro_regime import (
    TimedHorizonMacroRegimeStrategy,
)


CONFIG = BacktestConfig(
    starting_equity=100_000.0,
    risk_fraction=0.01,
    max_hold_bars=24,
    kill_switch_drawdown_fraction=None,
    slippage_bps=2.0,
    commission_per_trade=1.0,
)


def make_strategy(macro_frame) -> TimedHorizonMacroRegimeStrategy:
    return TimedHorizonMacroRegimeStrategy(
        macro=macro_frame,
        real_yield_lookback_days=10,
        real_yield_max_change_bps=0.0,
        vix_lookback_days=5,
        vix_max_change_abs=2.5,
        require_dxy_flat=True,
        dxy_lookback_days=20,
        dxy_max_abs_change_pct=1.0,
        far_atr_mult=8.0,
        once_per_day=True,
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
    fields = ["timestamp_run", "signal_bar_ts", "tf", "side",
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
                   help="directory containing <symbol>_<tf>.csv")
    p.add_argument("--symbol", default="xauusd")
    p.add_argument("--tf", default="60m", choices=["60m", "240m"],
                   help="primary timeframe (validated: 60m or 240m)")
    p.add_argument("--macro-cache-dir", default=str(REPO_ROOT / "data" / "macro"))
    p.add_argument("--journal", default=str(REPO_ROOT / "logs" / "macro_paper_journal.csv"))
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    sym = args.symbol.lower()
    primary_csv = data_dir / f"{sym}_{args.tf}.csv"
    macro_dir = Path(args.macro_cache_dir)
    journal_path = Path(args.journal)
    latest_path = data_dir / f"macro_latest_signal_{args.tf}.json"
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not primary_csv.exists():
        print(f"macro_paper_signal: missing {primary_csv}")
        return 0
    if not macro_dir.exists():
        print(f"macro_paper_signal: missing macro cache {macro_dir}")
        return 0

    primary = load_bars_from_csv(primary_csv)
    if len(primary) < 100:
        print(f"macro_paper_signal: insufficient bars ({len(primary)})")
        return 0

    macro = load_macro_frame(macro_dir)
    required = {"real10y", "dxy", "vix"}
    missing = required - set(macro.names())
    if missing:
        print(f"macro_paper_signal: macro frame missing required series: {sorted(missing)}")
        return 0

    strategy = make_strategy(macro)
    res = run_backtest(primary, strategy, CONFIG)

    last_bar_ts = primary[-1].timestamp.isoformat()
    if not res.trades:
        latest_path.write_text(json.dumps({
            "bar_ts": last_bar_ts,
            "tf": args.tf,
            "side": None,
            "generated_at": now_iso,
            "n_trades_in_window": 0,
            "note": "no signal in current data window",
        }, indent=2))
        print(f"macro_paper_signal[{args.tf}]: no signal at last bar {last_bar_ts}")
        return 0

    last_trade = res.trades[-1]
    signal_bar_ts = last_trade.entry_time.isoformat()
    rr = (last_trade.target - last_trade.entry_price) / max(
        abs(last_trade.entry_price - last_trade.stop), 1e-9
    )

    latest = {
        "bar_ts": last_bar_ts,
        "signal_bar_ts": signal_bar_ts,
        "tf": args.tf,
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
        print(f"macro_paper_signal[{args.tf}]: latest signal {signal_bar_ts} "
              f"already journaled (no-op)")
        return 0

    _journal_append(journal_path, {
        "timestamp_run": now_iso,
        "signal_bar_ts": signal_bar_ts,
        "tf": args.tf,
        "side": last_trade.side.name,
        "entry": f"{last_trade.entry_price:.5f}",
        "stop": f"{last_trade.stop:.5f}",
        "target": f"{last_trade.target:.5f}",
        "rr": f"{abs(rr):.3f}",
    })
    print(f"macro_paper_signal[{args.tf}]: APPENDED {last_trade.side.name} @ "
          f"{last_trade.entry_price:.2f} sl={last_trade.stop:.2f} "
          f"tp={last_trade.target:.2f} bar={signal_bar_ts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
