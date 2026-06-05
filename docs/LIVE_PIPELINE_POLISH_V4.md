# Gold Trader — Live Pipeline Polish v4

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


This patch addresses the remaining rough edges after v3:

- M1 may remain 0 because Twelve Data/free-plan 1min pulls can fail with large output size.
- Volatility could appear as ugly legacy terms like `dead`.
- Provider health needed candle counts by timeframe and real fetch errors.
- Blockers needed deduplication and operator-friendly wording.
- Quote/spread errors should stay nonfatal in paper mode.
- The repair layer should prefer the same repo Twelve Data provider that `/api/candles` uses, then fall back to direct CSV.

## Key behavior

- M1 fetch count is capped at 120 by default.
- Candles are cached under `data/cache/twelvedata`.
- Missing M1 is shown as a data-coverage issue, not a fake market signal.
- Volatility is computed from M15 ATR and normalized to `compressed`, `normal`, or `extreme`.
- The system still does not infer IFVG from repaired candles.

## Render command

```bash
PYTHONPATH=src python3 scripts/render_live_pipeline_repaired.py
```
