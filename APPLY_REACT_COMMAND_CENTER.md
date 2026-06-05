# Apply React Command Center

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


From repo root:

```bash
cd ~/Gold-Trader
unzip ~/Downloads/gold_trader_react_command_center.zip -d /tmp/react_cc
cp -R /tmp/react_cc/* .
chmod +x scripts/render_react_command_center.py

PYTHONPATH=src python3 -m gold_trader.web.react_command_center
# open http://localhost:8770
```

Stop with Ctrl+C, then test the Render runner:

```bash
PYTHONPATH=src python3 scripts/render_react_command_center.py
```

Commit:

```bash
git add frontend src/gold_trader/web/react_command_center.py scripts/render_react_command_center.py docs/REACT_COMMAND_CENTER.md APPLY_REACT_COMMAND_CENTER.md
git commit -m "Add React command center frontend"
git push
```

Render start command:

```bash
PYTHONPATH=src python3 scripts/render_react_command_center.py
```
