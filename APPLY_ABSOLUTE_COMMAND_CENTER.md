# Apply Absolute Gold Command Center

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


From repo root:

```bash
unzip gold_trader_absolute_command_center.zip -d /tmp/absolute_gold
cp -R /tmp/absolute_gold/* .
chmod +x scripts/render_absolute_gold.py
PYTHONPATH=src python3 -m gold_trader.web.command_center
```

Stop with Ctrl+C, then run the combined cloud runner:

```bash
PYTHONPATH=src python3 scripts/render_absolute_gold.py
```

Commit:

```bash
git add src/gold_trader/web/command_center.py scripts/render_absolute_gold.py docs/ABSOLUTE_GOLD_COMMAND_CENTER.md APPLY.md
git commit -m "Upgrade to absolute gold command center"
git push
```

Render start command:

```bash
PYTHONPATH=src python3 scripts/render_absolute_gold.py
```

Keep:

```text
GOLD_EXECUTION_MODE=paper
GOLD_ENABLE_LIVE_ORDERS=false
```
