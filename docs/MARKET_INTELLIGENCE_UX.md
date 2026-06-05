# Market Intelligence UX Hardening

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


This layer makes the command center more honest and operator-friendly.

## Adds to decision JSON

- `score_decomposition`
- `score_decomposition_total`
- `data_quality_penalty`
- `source_age_status`
- `missing_inputs`
- `readable_blockers`
- `readable_reasons`
- `market_intelligence_summary`
- `provider_health_summary`
- `chart_meta`

## Scoring model

The hardener decomposes score into:

| Component | Max |
|---|---:|
| Timeframe Alignment | 25 |
| IFVG Geometry | 20 |
| Macro Regime | 20 |
| Sentiment Gate | 15 |
| Session / Spread | 10 |
| Volatility | 10 |

Missing inputs are penalised and listed. Unknown macro/sentiment/spread should not produce Grade A.

## Provider health

The API exposes:

```text
/api/provider-health
/api/market-intelligence
/api/decision
/api/candles?tf=M15
```

## UI goal

The frontend should treat this as the truthful state contract. Do not infer score components in JavaScript.
