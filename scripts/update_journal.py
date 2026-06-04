"""Update the trade journal from the latest paper_state.json.

Designed to be invoked at the end of every cron `agent-cycle` run.
Idempotent — only appends rows whose closed_at is not already present.

Usage::

    python scripts/update_journal.py \
        --paper-state data/agent_live_xauusd/paper_state.json \
        --journal logs/trade_journal.csv \
        --bars data/agent_live_xauusd/xauusd_15m.csv \
        --macro-dir data/macro
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gold_trader.journal import update_journal  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--paper-state", type=Path, required=True)
    p.add_argument("--journal", type=Path, default=REPO / "logs" / "trade_journal.csv")
    p.add_argument("--bars", type=Path, default=None,
                   help="15m bar CSV used to compute regime tags at entry time.")
    p.add_argument("--macro-dir", type=Path, default=REPO / "data" / "macro")
    args = p.parse_args()

    n = update_journal(
        paper_state_path=args.paper_state,
        journal_path=args.journal,
        bars_csv=args.bars,
        macro_dir=args.macro_dir if args.macro_dir.exists() else None,
    )
    if n > 0:
        print(f"trade_journal: appended {n} row(s) to {args.journal}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
