# Gold Trader — Live Pipeline Repair

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


This patch fixes the remaining live-state problems visible on Render:

- `/api/provider-health` said Twelve Data was unknown even though candles were loading.
- M30 and other timeframe reads could remain `candles: 0` even when `/api/candles` worked.
- Volatility stayed `unknown` although M15 candles were available.
- Macro remained `unknown` without clear FMP diagnostics.
- Score decomposition and missing-input penalties needed to be written into the actual decision JSON, not just inferred in the UI.
- Unsafe ready states are downgraded when hard blocks exist.

## New files

```text
src/gold_trader/core/live_pipeline_repair.py
scripts/repair_live_pipeline.py
scripts/render_live_pipeline_repaired.py
```

## Render start command

```bash
PYTHONPATH=src python3 scripts/render_live_pipeline_repaired.py
```

## Required environment

```text
TWELVE_DATA_API_KEY=...
FMP_API_KEY=...
GOLD_EXECUTION_MODE=paper
GOLD_ENABLE_LIVE_ORDERS=false
GOLD_STRICT_UNKNOWN_CONTEXT=true
```

Optional:

```text
GOLD_MACRO_BLOCK_BEFORE_MINUTES=45
GOLD_MACRO_BLOCK_AFTER_MINUTES=30
GOLD_DECISION_STALE_SECONDS=300
GOLD_MIN_RR_TP2=2.0
GOLD_GRADE_A_MIN_SCORE=82
```

## Expected API improvements

`/api/provider-health` should show:

```json
{
  "twelvedata": {"state": "ok", "candles_loaded": 3500},
  "volatility": {"state": "normal"},
  "spread": {"state": "ok or unknown_nonfatal_in_paper"},
  "fmp_macro": {"state": "clear, blocked, missing_key, or error"}
}
```

`/api/decision` should include:

```json
{
  "score_decomposition": {},
  "data_quality_penalty": 0,
  "missing_inputs": [],
  "hard_blocks": [],
  "watching_for": [],
  "tf_alignment_audit": {}
}
```
