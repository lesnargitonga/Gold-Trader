# Apply Absolute Gold App

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


```bash
cd ~/Gold-Trader
unzip ~/Downloads/gold_trader_absolute_gold_app.zip -d /tmp/absolute_gold_app
cp -R /tmp/absolute_gold_app/* .
chmod +x scripts/render_absolute_gold_app.py

PYTHONPATH=src python3 -m gold_trader.web.absolute_gold_app
# open http://localhost:8770 then Ctrl+C

PYTHONPATH=src python3 scripts/render_absolute_gold_app.py
# open http://localhost:8770 then Ctrl+C

git add src/gold_trader/web/absolute_gold_app.py scripts/render_absolute_gold_app.py docs/ABSOLUTE_GOLD_APP.md APPLY_ABSOLUTE_GOLD_APP.md
git commit -m "Build Absolute Gold multi-page command center"
git push
```

Render Start Command:

```bash
PYTHONPATH=src python3 scripts/render_absolute_gold_app.py
```
