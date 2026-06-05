# Render Frontend Mode

Current system truth is defined in `docs/SYSTEM_TRUTH.md`. If this file conflicts with it, `docs/SYSTEM_TRUTH.md` wins.

Gold Trader uses Render as the official remote dashboard and keeps the local PC as the authoritative trading and data engine.

## Active Render Path

The current Render-facing command center is:

- `scripts/render_live_pipeline_repaired.py`
- `scripts/render_market_intelligence_ux.py`
- `src/gold_trader/web/market_intelligence_api.py`
- `frontend/market_intelligence/command_center.js`

The local-only UI remains:

- `src/gold_trader/web/server.py`
- `src/gold_trader/web/static/index.html`
- `src/gold_trader/web/static/app.js`

Do not add another frontend for this mode.

## State Sync Contract

The local stack writes canonical cloud-readable state under:

```text
data/cloud_state/
```

The main bundle is:

```text
data/cloud_state/latest_cloud_state.json
```

The bundle is built from local authoritative files such as:

- `logs/ifvg_mtf_decision_state.json`
- `logs/provider_health.json`
- `logs/paper_performance_report.json`
- `logs/paper_signal_outcomes.jsonl`
- `logs/decision_journal.jsonl`

The local publisher guarantees:

- `source = "local_authoritative_engine"`
- `live_allowed = false`
- `live_orders_enabled = false`
- `cloud_status.broker = "MT5 bridge local"`
- `cloud_status.orders = "locked"`
- `market_context.spread_source = "live_tick"` when the local bridge supplied the spread
- `data_health.spread = "live_tick"` when the local bridge supplied the spread

## Render Ingest

Render accepts synced state at:

```text
POST /api/ingest-state
```

It requires:

```text
GOLD_CLOUD_SYNC_TOKEN
X-Gold-Sync-Token: <same token>
```

Local publishing uses:

```bash
PYTHONPATH=src .venv/bin/python scripts/publish_state_to_render_payload.py
GOLD_RENDER_INGEST_URL="https://gold-trader-kmaw.onrender.com/api/ingest-state" \
GOLD_CLOUD_SYNC_TOKEN="..." \
PYTHONPATH=src .venv/bin/python scripts/publish_state_to_render.py
```

`scripts/ifvg_auto_scout.py` builds the payload every interval. It posts to Render only when both `GOLD_RENDER_INGEST_URL` and `GOLD_CLOUD_SYNC_TOKEN` are present.

## Render API Parity

These endpoints always return JSON:

- `/api/decision`
- `/api/performance`
- `/api/paper-signals`
- `/api/decision-journal`
- `/api/provider-health`

Before the first sync, Render reports cloud fallback/missing state. It must not claim MT5, live tick, or local-authoritative data until local state has been ingested.

If the latest published cloud state is older than 300 seconds, the UI shows:

```text
Cloud state stale — local engine not syncing
```

## Safety

Live orders remain locked in Render mode. This dashboard is for supervised paper collection, monitoring, and evidence review only.
