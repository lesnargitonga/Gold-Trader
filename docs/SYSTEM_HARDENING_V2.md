# Gold Trader — System Hardening v2

This patch fixes the missing market-awareness pipeline issues that made the UI look better while the engine still had blind spots.

## What it adds

- FMP economic calendar fetcher: `scripts/fetch_calendar.py`
- Decision hardening/enrichment: `scripts/harden_market_state.py`
- Provider health generator: `scripts/provider_health.py`
- Render runner that executes calendar → live context → IFVG engine → merge → hardening: `scripts/render_hardened_market_awareness.py`
- Core hardening module: `src/gold_trader/core/market_state_hardening.py`

## Fixed issues

1. Economic calendar no longer stays as an invisible missing dependency. It writes `data/macro/economic_calendar.json` and records provider failure states.
2. Missing spread, macro, sentiment, volatility, and stale source age are explicitly penalized.
3. Trade-ready cannot remain Grade A when required context is missing or hard blocks are active.
4. Timeframe alignment is audited strictly with per-TF details.
5. `tf_align`, `watching_for`, `score_decomposition`, `data_quality_penalty`, `missing_inputs`, `hard_blocks`, and `provider_health` are written into state.
6. CTrader/CME/options are shown truthfully as pending or not connected unless credentials exist.
7. Decision snapshots are appended to `data/journal/decision_snapshots.jsonl` for forward-test evidence.

## Render start command

```bash
PYTHONPATH=src python3 scripts/render_hardened_market_awareness.py
```

## Required env vars

```text
TWELVE_DATA_API_KEY=...
FMP_API_KEY=...
FINNHUB_API_KEY=...
GOLD_EXECUTION_MODE=paper
GOLD_ENABLE_LIVE_ORDERS=false
GOLD_STRICT_UNKNOWN_CONTEXT=true
GOLD_RENDER_SCOUT_INTERVAL_SECONDS=300
```

Optional policy tuning:

```text
GOLD_REQUIRED_ALIGNED_TFS=5
GOLD_REQUIRED_HTF_TFS=2
GOLD_GRADE_A_SCORE=82
GOLD_MIN_RR_TP2=2.0
GOLD_MAX_DECISION_AGE_SECONDS=300
GOLD_MISSING_INPUT_PENALTY=8
GOLD_MAX_SPREAD_POINTS=80
```

## State fields produced

```json
{
  "score_decomposition": {
    "timeframe_alignment": 0,
    "ifvg_geometry": 0,
    "macro_regime": 0,
    "sentiment_gate": 0,
    "session_spread": 0,
    "volatility": 0
  },
  "data_quality_penalty": 0,
  "missing_inputs": [],
  "hard_blocks": [],
  "warnings": [],
  "tf_align": {"D1": "bearish"},
  "tf_alignment_audit": {},
  "watching_for": [],
  "provider_health": {},
  "source_age_seconds": 0,
  "source_age_status": "fresh"
}
```

## Important

This patch intentionally keeps live execution locked. It hardens analysis and paper alerts only.
