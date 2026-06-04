# React Command Center v3

This patch replaces the fragile React command center entry with a robust, no-build React cockpit served by Python.

It fixes:
- decision JSON discovery from multiple known paths
- hero verdict binding (`TRADE_READY...` becomes `PAPER TRADE READY`)
- score / grade / side / entry / stop / targets display
- timeframe switching for D1, H4, H1, M30, M15, M5, M1
- `/api/candles?tf=...` directly from Twelve Data
- visible source age and candle count
- runner sequence: update context → run IFVG engine → merge context → serve UI

## Frontend build

Source: `frontend/react_command_center_v3/app.jsx` (compiled to `app.js` before deploy):

```bash
bash scripts/compile_react_command_center.sh
```

## Render start command

```bash
PYTHONPATH=src python3 scripts/render_react_command_center_v3.py
```

Keep live execution locked:

```text
GOLD_EXECUTION_MODE=paper
GOLD_ENABLE_LIVE_ORDERS=false
```
