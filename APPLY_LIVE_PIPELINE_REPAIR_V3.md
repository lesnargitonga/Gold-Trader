# Apply Live Pipeline Repair v3

```bash
cd ~/Gold-Trader
unzip ~/Downloads/gold_trader_live_pipeline_repair_v3.zip -d /tmp/gold_live_repair_v3
cp -R /tmp/gold_live_repair_v3/* .
chmod +x scripts/repair_live_pipeline.py scripts/render_live_pipeline_repaired.py

PYTHONPATH=src python3 scripts/repair_live_pipeline.py
```

Commit:

```bash
git add src/gold_trader/core/live_pipeline_repair.py \
        scripts/repair_live_pipeline.py \
        scripts/render_live_pipeline_repaired.py \
        docs/LIVE_PIPELINE_REPAIR_V3.md \
        APPLY_LIVE_PIPELINE_REPAIR_V3.md

git commit -m "Fix live pipeline state regression"
git push
```

Render start command:

```bash
PYTHONPATH=src python3 scripts/render_live_pipeline_repaired.py
```

Verify:

```bash
curl -s https://gold-trader-kmaw.onrender.com/api/provider-health | head -c 2000
curl -s https://gold-trader-kmaw.onrender.com/api/decision | grep -E '"candles": 0|"current_price": null|"candles_loaded"|score_decomposition|missing_inputs'
```
