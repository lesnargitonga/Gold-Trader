# Absolute Gold — Full Market Awareness Layer

This patch adds the cloud context layer that fills the remaining gaps around the IFVG engine.

## Providers

Required for live context:

```text
TWELVE_DATA_API_KEY=...
FMP_API_KEY=...
FINNHUB_API_KEY=...
GOLD_SYMBOL=XAU/USD
GOLD_EXECUTION_MODE=paper
GOLD_ENABLE_LIVE_ORDERS=false
```

Optional:

```text
GOLD_TWELVE_DATA_SYMBOL=XAU/USD
GOLD_MAX_SPREAD_POINTS=1.5
GOLD_MACRO_BLOCK_MINUTES_BEFORE=45
GOLD_MACRO_BLOCK_MINUTES_AFTER=45
GOLD_CONTEXT_SYMBOLS=DXY,US10Y,VIX,SPY
GOLD_ALERTS_ENABLED=true
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Files produced

```text
logs/live_market_context.json
logs/spread_state.json
logs/sentiment_state.json
data/macro/economic_calendar.json
data/cot/gold_cot_state.json
logs/cross_market_state.json
```

## Render start command

Recommended (React UI + full context scout):

```bash
PYTHONPATH=src python3 scripts/render_react_command_center_v2.py
```

Python-only UI:

```bash
PYTHONPATH=src python3 scripts/render_absolute_gold_full_context.py
```

## Safety

The context layer may block or warn, but it never places orders. Keep live orders locked until cTrader is approved and forward-test performance is proven:

```text
GOLD_ENABLE_LIVE_ORDERS=false
```
