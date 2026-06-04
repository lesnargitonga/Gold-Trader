#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from gold_trader.core.market_state_hardening import load_decision, load_live_context, decision_age_seconds, provider_health


if __name__ == "__main__":
    decision, source = load_decision()
    context = load_live_context()
    age = decision_age_seconds(decision, source)
    print(json.dumps(provider_health(decision, context, source, age), indent=2))
