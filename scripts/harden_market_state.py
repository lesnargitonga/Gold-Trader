#!/usr/bin/env python3
from __future__ import annotations

import json

from gold_trader.core.market_state_hardening import harden_decision_state


if __name__ == "__main__":
    print(json.dumps(harden_decision_state(), indent=2, allow_nan=False))
