# Apply Live Pipeline Repair

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


```bash
cd ~/Gold-Trader
unzip ~/Downloads/gold_trader_live_pipeline_repair_v2.zip -d /tmp/gold_live_repair
cp -R /tmp/gold_live_repair/* .
chmod +x scripts/repair_live_pipeline.py scripts/render_live_pipeline_repaired.py

PYTHONPATH=src python3 scripts/repair_live_pipeline.py
```

Commit:

```bash
git add src/gold_trader/core/live_pipeline_repair.py \
        scripts/repair_live_pipeline.py \
        scripts/render_live_pipeline_repaired.py \
        docs/LIVE_PIPELINE_REPAIR.md \
        APPLY_LIVE_PIPELINE_REPAIR.md

git commit -m "Repair live market pipeline and provider health"
git push
```

Render start command:

```bash
PYTHONPATH=src python3 scripts/render_live_pipeline_repaired.py
```
