#!/usr/bin/env python3
from __future__ import annotations

import json

from gold_trader.core.market_intelligence_ux import harden_decision


if __name__ == "__main__":
    print(json.dumps(harden_decision(), indent=2, allow_nan=False))
