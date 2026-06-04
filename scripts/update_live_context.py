#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
os.environ.setdefault("GOLD_RUNTIME_ROOT", str(ROOT))

from gold_trader.data.live_context import build_live_context


def main() -> None:
    symbol = os.getenv("GOLD_TWELVE_DATA_SYMBOL") or os.getenv("GOLD_SYMBOL", "XAU/USD")
    ctx = build_live_context(symbol)
    print(json.dumps(ctx.__dict__, indent=2, default=str))


if __name__ == "__main__":
    main()
