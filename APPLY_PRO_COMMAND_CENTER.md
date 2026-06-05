# Apply

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


```bash
cd ~/Gold-Trader
unzip ~/Downloads/gold_trader_pro_command_center.zip -d /tmp/procc
cp -R /tmp/procc/* .
chmod +x scripts/render_pro_command_center.py
PYTHONPATH=src python3 -m gold_trader.web.pro_command_center
```

Open `http://localhost:8770`.

Commit:

```bash
git add src/gold_trader/web/pro_command_center.py scripts/render_pro_command_center.py docs/PRO_COMMAND_CENTER.md APPLY_PRO_COMMAND_CENTER.md
git commit -m "Upgrade professional React command center"
git push
```

Render start command:

```bash
PYTHONPATH=src python3 scripts/render_pro_command_center.py
```
