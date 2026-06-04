# React Command Center

This patch replaces the long server-rendered command page with a React cockpit served by the existing Python service.

## What it adds

- Multi-page React frontend: Trade Cockpit, Market Context, Signal Engine, Risk & Orders, Journal, Settings.
- Live candlestick workbench with timeframe switching: D1, H4, H1, M30, M15, M5, M1.
- Large clear verdict hero for WAIT / PAPER TRADE READY / BUY / SELL states.
- `/api/decision`, `/api/candles`, `/api/alerts`, `/health` endpoints.
- No build step and no Node dependency: React is loaded from CDN, and the Python server serves the app.
- Render runner that keeps the full-system IFVG loop running in the background.

## Render start command (pro recommended)

```bash
PYTHONPATH=src python3 scripts/render_pro_command_center.py
```

Older runners (`render_react_command_center*.py`) delegate to the pro command center.

See [PRO_COMMAND_CENTER.md](PRO_COMMAND_CENTER.md) for the professional cockpit.

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

## Safety

The UI never enables live orders. Live trading remains controlled by backend policy and `GOLD_ENABLE_LIVE_ORDERS`.
