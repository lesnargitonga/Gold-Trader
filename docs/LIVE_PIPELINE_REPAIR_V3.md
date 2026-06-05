# Gold Trader — Live Pipeline Repair v3

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


This fixes the regression where the dashboard showed `candles: 0` and no current price even though `/api/candles?tf=M15` worked.

## Root cause

The previous runner could publish a placeholder repaired state before the full IFVG engine finished. It could also choose `data/state.json` over the real engine decision, causing zero-candle placeholders to replace the good state.

## Fixes

- Runner now executes full cycle before serving:
  1. update live context
  2. run IFVG full-system engine
  3. merge live context
  4. repair/harden decision
- Decision loader chooses the highest-quality decision, not blindly `data/state.json`.
- Candle repair prefers the already-working repo Twelve Data provider used by `/api/candles`.
- Provider health reports Twelve Data as OK when decision candles are present.
- Volatility computes from M15 candles.
- Quote/spread endpoint errors are nonfatal in paper mode.
- The repair layer never infers IFVG from candles; it only repairs candle counts, current price, and simple bias when missing.

## Render start command

```bash
PYTHONPATH=src python3 scripts/render_live_pipeline_repaired.py
```
