# Apply Live Pipeline Polish v4

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


```bash
cd ~/Gold-Trader
unzip ~/Downloads/gold_trader_live_pipeline_polish_v4.zip -d /tmp/gold_live_polish_v4
cp -R /tmp/gold_live_polish_v4/* .
chmod +x scripts/repair_live_pipeline.py scripts/render_live_pipeline_repaired.py

PYTHONPATH=src python3 scripts/repair_live_pipeline.py
```

Commit:

```bash
git add src/gold_trader/core/live_pipeline_repair.py \
        scripts/repair_live_pipeline.py \
        scripts/render_live_pipeline_repaired.py \
        docs/LIVE_PIPELINE_POLISH_V4.md \
        APPLY_LIVE_PIPELINE_POLISH_V4.md

git commit -m "Polish live pipeline health and blockers"
git push
```

Render start command:

```bash
PYTHONPATH=src python3 scripts/render_live_pipeline_repaired.py
```

Verify:

```bash
curl -s https://gold-trader-kmaw.onrender.com/api/provider-health | head -c 4000
curl -s https://gold-trader-kmaw.onrender.com/api/decision | grep -E '"M1"|"candles_loaded"|score_decomposition|missing_inputs|volatility'
```
