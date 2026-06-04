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
os.environ.setdefault("GOLD_TRADER_ROOT", str(ROOT))
os.environ.setdefault("GOLD_REPO_ROOT", str(ROOT))

from gold_trader.core.live_pipeline_repair import repair_and_harden


if __name__ == "__main__":
    print(json.dumps(repair_and_harden(), indent=2, allow_nan=False, default=str))
