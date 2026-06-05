# Gold Trader System Truth

This is the current operating truth for Gold Trader. If another document conflicts with this page, this page wins.

## Authority Split

- There is exactly one trading truth: `data/cloud_state/latest_cloud_state.json` published by the local authoritative engine.
- Local PC is the authoritative trading and market-data engine.
- Render is the official remote dashboard for mirrored state.
- Render is not the trading brain and does not talk directly to the local MT5 bridge.
- Render must not compute trading decisions.
- Render must not use Twelve Data, Yahoo, FMP, Finnhub, or any other cloud provider to create or modify trading state.
- Live orders remain locked.
- Paper collection is allowed only from the local authoritative decision state.

## Official Local Stack

Start the local stack with:

```bash
bash scripts/start.sh
```

Local services:

- MT5 bridge: `http://127.0.0.1:8765`
- Web UI/API: `http://127.0.0.1:8770`

Official local UI files:

- `src/gold_trader/web/server.py`
- `src/gold_trader/web/static/index.html`
- `src/gold_trader/web/static/app.js`

Canonical local decision file:

```text
logs/ifvg_mtf_decision_state.json
```

The local scout loop refreshes the authoritative state with:

```text
scripts/update_live_inputs.py
scripts/ifvg_full_system_engine.py
scripts/journal_decision_snapshot.py
scripts/update_paper_signal_outcomes.py
scripts/report_paper_performance.py
scripts/publish_state_to_render_payload.py
```

If `GOLD_RENDER_INGEST_URL` and `GOLD_CLOUD_SYNC_TOKEN` are present, the scout also runs:

```text
scripts/publish_state_to_render.py
```

## Official Render Dashboard

Render serves the current remote dashboard through:

- `scripts/render_live_pipeline_repaired.py`
- `scripts/render_market_intelligence_ux.py`
- `src/gold_trader/web/market_intelligence_api.py`
- `frontend/market_intelligence/command_center.js`

Render reads synced state from:

```text
data/cloud_state/latest_cloud_state.json
```

Render must report `no_valid_local_state` until fresh local authoritative state has been ingested.
If cloud sync is missing or older than 300 seconds, the dashboard must show:

```text
LOCAL ENGINE NOT SYNCING
```

## Cloud Sync Contract

Local publisher:

```bash
PYTHONPATH=src .venv/bin/python scripts/publish_state_to_render_payload.py
PYTHONPATH=src .venv/bin/python scripts/publish_state_to_render.py
```

Required env:

```text
GOLD_RENDER_INGEST_URL=https://gold-trader-kmaw.onrender.com/api/ingest-state
GOLD_CLOUD_SYNC_TOKEN=<shared secret>
```

Render ingest endpoint:

```text
POST /api/ingest-state
X-Gold-Sync-Token: <shared secret>
```

Render API parity endpoints:

- `/api/decision`
- `/api/performance`
- `/api/paper-signals`
- `/api/decision-journal`
- `/api/provider-health`

When synced, `/api/decision` must expose:

```json
{
  "source": "local_authoritative_engine",
  "cloud_sync": "fresh",
  "live_allowed": false,
  "live_orders_enabled": false,
  "market_context": {
    "spread_source": "live_tick"
  },
  "data_health": {
    "spread": "live_tick"
  },
  "cloud_status": {
    "broker": "MT5 bridge local",
    "orders": "locked",
    "data_provider": "local_authoritative_engine"
  }
}
```

If cloud state is older than 300 seconds, the dashboard must show:

```text
LOCAL ENGINE NOT SYNCING
```

## Safety Rules

- Do not enable live orders.
- Do not add another frontend.
- Do not tune signal thresholds from one signal.
- Do not use placeholder data to satisfy trading gates.
- Do not claim MT5, live tick, or local-authoritative data on Render unless synced local state proves it.
- Do not use Render provider-key gaps as trading blockers when fresh synced local state exists.
- Render provider/chart data is chart preview only and never trading truth.

## Historical Docs

Older APPLY docs and old Command Center docs are implementation history. They are not current operating instructions unless this page links to them as active truth.

Use these current docs for operations:

- `docs/SYSTEM_TRUTH.md`
- `docs/RENDER_FRONTEND_MODE.md`
- `docs/FINAL_LOCAL_OPERATION.md`
- `docs/PAPER_JOURNAL_AND_PERFORMANCE.md`
