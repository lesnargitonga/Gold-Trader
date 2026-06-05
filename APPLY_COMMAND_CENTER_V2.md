# Apply Command Center v2

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


```bash
cd ~/Gold-Trader
unzip ~/Downloads/gold_trader_command_center_v2.zip -d /tmp/ccv2
cp -R /tmp/ccv2/* .
chmod +x scripts/render_command_center_v2.py

PYTHONPATH=src python3 -m gold_trader.web.command_center_v2
# open http://localhost:8770
```

Commit:

```bash
git add src/gold_trader/web/command_center_v2.py scripts/render_command_center_v2.py docs/COMMAND_CENTER_V2.md APPLY_COMMAND_CENTER_V2.md
git commit -m "Upgrade command center v2 market cockpit"
git push
```

Render start command:

```bash
PYTHONPATH=src python3 scripts/render_command_center_v2.py
```
