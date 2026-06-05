# Final Local Operation

The official local UI is served by:

- `src/gold_trader/web/server.py`
- `src/gold_trader/web/static/index.html`
- `src/gold_trader/web/static/app.js`

Start it with:

```bash
bash scripts/start.sh
```

The `frontend/` directory and older command-center prototypes are deprecated unless explicitly reactivated.
The local PC is the authoritative trading engine. Render is a mirror/fallback dashboard only.
Live orders remain locked unless explicitly enabled by policy and environment.

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
LOCAL AUTHORITATIVE MODE
Paper mode — live orders locked
Broker: MT5 bridge local · cTrader pending
```
Then verify APIs:

```bash
curl -sS http://127.0.0.1:8770/api/decision | jq '.action,.final_score,.market_context,.blockers'
curl -sS http://127.0.0.1:8770/api/performance | jq
curl -sS http://127.0.0.1:8770/api/decision-journal | jq '.items[:3]'
```
