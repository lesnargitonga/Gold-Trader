# Apply React Command Center v2

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


```bash
cd ~/Gold-Trader
unzip ~/Downloads/gold_trader_react_command_center_v2.zip -d /tmp/react_cc_v2
cp -R /tmp/react_cc_v2/* .
chmod +x scripts/render_react_command_center_v2.py

PYTHONPATH=src python3 -m gold_trader.web.react_command_center
# open http://localhost:8770
# Ctrl+C

PYTHONPATH=src python3 scripts/render_react_command_center_v2.py
# open http://localhost:8770
# Ctrl+C

git add frontend/react_command_center src/gold_trader/web/react_command_center.py scripts/render_react_command_center_v2.py docs/REACT_COMMAND_CENTER_V2.md APPLY_REACT_COMMAND_CENTER_V2.md
git commit -m "Polish React command center data loading"
git push
```

Render start command:

```bash
PYTHONPATH=src python3 scripts/render_react_command_center_v2.py
```

Required env vars:

```text
PYTHONPATH=src
GOLD_MARKET_DATA_PROVIDER=twelvedata
TWELVE_DATA_API_KEY=...
GOLD_SYMBOL=XAU/USD
GOLD_EXECUTION_MODE=paper
GOLD_ENABLE_LIVE_ORDERS=false
GOLD_RENDER_SCOUT_INTERVAL_SECONDS=300
```
