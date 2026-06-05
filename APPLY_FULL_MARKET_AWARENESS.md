# Apply Full Market Awareness Patch

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


```bash
cd ~/Gold-Trader
unzip ~/Downloads/gold_trader_full_market_awareness.zip -d /tmp/ag_context
cp -R /tmp/ag_context/* .
chmod +x scripts/update_live_context.py scripts/merge_live_context_into_decision.py scripts/render_absolute_gold_full_context.py

PYTHONPATH=src python3 scripts/update_live_context.py
PYTHONPATH=src python3 scripts/merge_live_context_into_decision.py || true

git add src/gold_trader/data/live_context.py src/gold_trader/notify/telegram.py scripts/update_live_context.py scripts/merge_live_context_into_decision.py scripts/render_absolute_gold_full_context.py docs/FULL_MARKET_AWARENESS.md APPLY_FULL_MARKET_AWARENESS.md
git commit -m "Add full market awareness context layer"
git push
```

Render start command:

```bash
PYTHONPATH=src python3 scripts/render_absolute_gold_full_context.py
```
