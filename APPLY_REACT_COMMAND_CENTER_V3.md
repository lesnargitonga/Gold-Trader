# Apply React Command Center v3

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


```bash
cd ~/Gold-Trader
unzip ~/Downloads/gold_trader_react_command_center_v3.zip -d /tmp/react_cc_v3
cp -R /tmp/react_cc_v3/* .
chmod +x scripts/render_react_command_center_v3.py

PYTHONPATH=src python3 -m gold_trader.web.react_command_center
# open http://localhost:8770
# Ctrl+C

PYTHONPATH=src python3 scripts/render_react_command_center_v3.py
# Ctrl+C

git add src/gold_trader/web/react_command_center.py scripts/render_react_command_center_v3.py docs/REACT_COMMAND_CENTER_V3.md APPLY_REACT_COMMAND_CENTER_V3.md
git commit -m "Fix React command center live data binding"
git push
```

Render start command:

```bash
PYTHONPATH=src python3 scripts/render_react_command_center_v3.py
```
