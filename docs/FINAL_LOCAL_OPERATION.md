# Final Local Operation

Current system truth is defined in `docs/SYSTEM_TRUTH.md`. If this file conflicts with it, `docs/SYSTEM_TRUTH.md` wins.

The official local UI is the same Command Center frontend used by Render:

- `src/gold_trader/web/market_intelligence_api.py`
- `frontend/market_intelligence/command_center.js`

Start it with:

```bash
bash scripts/start.sh
```

The local PC is the authoritative trading engine.
Render is the official remote dashboard, but it only mirrors synced local state and must not act as the trading brain.
The `frontend/market_intelligence/command_center.js` file is the active dashboard UI locally and on Render; older command-center prototypes and the old static local UI are historical.
Live orders remain locked.

## Verify

```bash
bash scripts/stop.sh
bash scripts/start.sh
```
Open:

```
http://127.0.0.1:8770
```
Check that it says:

```
RENDER DASHBOARD MODE
Source: local authoritative engine
Orders locked
Cloud sync fresh
```

For Render dashboard verification:

```bash
curl -sS https://gold-trader-kmaw.onrender.com/api/decision \
  | jq '.source,.cloud_sync.state,.cloud_status.broker,.cloud_status.orders,.market_context.spread_source,.data_health.spread,.live_orders_enabled'
```

Expected after sync:

```text
local_authoritative_engine
fresh
MT5 bridge local
locked
live_tick
live_tick
false
```
Then verify APIs:

```bash
curl -sS http://127.0.0.1:8770/api/decision | jq '.action,.final_score,.market_context,.blockers'
curl -sS http://127.0.0.1:8770/api/performance | jq
curl -sS http://127.0.0.1:8770/api/decision-journal | jq '.items[:3]'
```
