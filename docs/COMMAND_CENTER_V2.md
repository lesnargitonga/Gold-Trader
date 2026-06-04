# Gold Trader Command Center v2

Professional React-powered cockpit served by Python, designed around the existing Gold Trader backend.

## Correct data paths

This version reads the real backend files used by the current app:

- `logs/ifvg_mtf_decision_state.json`
- `logs/live_market_context.json`
- `logs/operator_alerts.jsonl`

It also accepts `data/state.json` as a fallback, but the production source remains the IFVG decision JSON.

## Added UX

- Score decomposition panel
- Source age chip with green / amber / red freshness dot
- Watching For panel
- Prominent Orders Locked / Ready CTA
- Data Issues panel
- Visual timeframe alignment grid
- Switchable live candlestick workbench: D1, H4, H1, M30, M15, M5, M1

## Frontend

UI lives in `frontend/command_center_v2/` (plain `React.createElement` — no build step required).

## Render

Use this start command:

```bash
PYTHONPATH=src python3 scripts/render_command_center_v2.py
```

Do **not** use `gunicorn app:app` unless you separately convert this to a Flask/FastAPI app. The current server is a dependency-light `ThreadingHTTPServer`.

Keep live orders locked until broker execution is tested:

```text
GOLD_EXECUTION_MODE=paper
GOLD_ENABLE_LIVE_ORDERS=false
```
