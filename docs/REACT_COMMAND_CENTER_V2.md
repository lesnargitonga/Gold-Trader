# React Command Center v2

This patch replaces the earlier React command center with a tighter, data-safe frontend and a more robust Python API server.

## Frontend build

`frontend/react_command_center/app.jsx` is the JSX source. Browsers cannot run JSX directly — compile before deploy:

```bash
bash scripts/compile_react_command_center.sh
```

This writes `app.js` (plain `React.createElement`) served at `/assets/app.js`.

## Key fixes

- Decision API now searches the Render/current working directory for `logs/ifvg_mtf_decision_state.json`.
- UI shows clear verdict, grade, score, source age, provider, and order lock.
- Timeframe switcher works through `/api/candles?tf=D1|H4|H1|M30|M15|M5|M1`.
- Chart panel reports real provider errors instead of silently appearing empty.
- The runner updates live context, runs the full-system engine, merges context, then serves the React UI.
- Live orders remain locked unless `GOLD_ENABLE_LIVE_ORDERS=true`.

## Render start command

```bash
PYTHONPATH=src python3 scripts/render_react_command_center_v2.py
```
