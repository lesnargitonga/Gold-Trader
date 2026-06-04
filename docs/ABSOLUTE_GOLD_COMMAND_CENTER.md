# Absolute Gold Command Center

This package is a production-grade UI overlay for the current Gold Trader backend.

## Goals

- No raw backend exceptions shown to the trader.
- Cloud data status is explicit.
- MT5 bridge is treated as optional local infrastructure, not a blocker for Render cloud analysis.
- Live orders remain locked unless explicitly enabled.
- Decision state, blockers, reasons, daily guard, market context, and timeframe reads are front and center.
- The UI is dependency-light: Python standard library + existing repo code.

## Render start command

Superseded by the multi-page app:

```bash
PYTHONPATH=src python3 scripts/render_absolute_gold_app.py
```

Legacy single-page UI:

```bash
PYTHONPATH=src python3 scripts/render_absolute_gold.py
```

## Required env

```text
PYTHONPATH=src
GOLD_MARKET_DATA_PROVIDER=twelvedata
TWELVE_DATA_API_KEY=...
GOLD_SYMBOL=XAU/USD
GOLD_EXECUTION_MODE=paper
GOLD_ENABLE_LIVE_ORDERS=false
GOLD_RENDER_SCOUT_INTERVAL_SECONDS=300
```

## Optional env

```text
GOLD_TWELVE_DATA_SYMBOL=XAU/USD
GOLD_TWELVE_DATA_CACHE_SECONDS=120
GOLD_RUN_LEGACY_SCOUT=false
GOLD_BROKER=ctrader
FMP_API_KEY=...
FINNHUB_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Endpoints

- `/` premium command center
- `/api/decision` sanitized decision JSON
- `/api/candles?tf=M15` chart candle feed
- `/api/alerts` latest alert JSONL entries
- `/health` Render health/status

## Safety

The runner defaults to paper mode and live-order lock. If live execution is ever added, the UI still displays whether orders are locked or unlocked.
