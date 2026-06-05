# Gold Trader Professional Command Center

> **Historical note:** This file records an older implementation step. Current operating truth is `docs/SYSTEM_TRUTH.md`; do not use this as current run or deploy guidance.


This package replaces the current React command center with a more complete professional cockpit served by Python.

## Features
- Multi-page navigation: Trade Cockpit, Market Context, Signal Engine, Risk & Orders, Journal, Settings, Decision JSON.
- Strong verdict hero with normalized `TRADE_READY_*` actions.
- Switchable live candlestick workbench: D1, H4, H1, M30, M15, M5, M1.
- Full timeframe alignment grid.
- Live context, risk guard, alerts, and JSON diagnostics.
- Reads the real backend decision JSON from multiple known paths.
- Does not expose raw legacy scout errors in the trader UI.

## Frontend build

```bash
bash scripts/compile_react_command_center.sh
```

Compiles `frontend/pro_command_center/app.jsx` → `app.js` (required — browsers cannot run raw JSX).

## Render start command

```bash
PYTHONPATH=src python3 scripts/render_pro_command_center.py
```

Keep live orders locked unless deliberately testing approved broker execution:

```text
GOLD_EXECUTION_MODE=paper
GOLD_ENABLE_LIVE_ORDERS=false
```
