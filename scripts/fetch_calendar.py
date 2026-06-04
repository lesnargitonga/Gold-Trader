#!/usr/bin/env python3
from __future__ import annotations

import json

from gold_trader.core.market_state_hardening import fetch_fmp_calendar


if __name__ == "__main__":
    print(json.dumps(fetch_fmp_calendar(), indent=2))
