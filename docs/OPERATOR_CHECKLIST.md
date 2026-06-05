# Gold Trader Local Operator Checklist

Current system truth is defined in `docs/SYSTEM_TRUTH.md`. If this file conflicts with it, `docs/SYSTEM_TRUTH.md` wins.

## Decision now
Use this mode for the next stable build:

```
PC = authoritative trading engine
Render = official remote dashboard mirror
Live orders = locked
```

Do not make Render the trading brain. Render must show synced local state, cloud fallback/missing, or stale sync warnings honestly.

---

## 1. Start clean

```bash
cd ~/Gold-Trader
source .venv/bin/activate
git pull
bash scripts/stop.sh
```

Then start the full local stack:

```bash
./scripts/start.sh
```

Open the UI:

```
http://127.0.0.1:8770
```

---

## 2. Confirm MT5 bridge is alive
Run:

```bash
curl -s http://127.0.0.1:8765/health
```

Then (authenticated if your bridge requires a secret):

```bash
curl -s -H "X-Gold-Bridge-Secret: <bridge-secret>" http://127.0.0.1:8765/last-tick | head -c 1000
```

You need to see real `bid`/`ask`. If `bid`/`ask` is missing, spread will remain broken.

---

## 3. Run full-system decision manually

```bash
PYTHONPATH=src \
GOLD_BRIDGE_URL="http://127.0.0.1:8765" \
.venv/bin/python scripts/ifvg_full_system_engine.py
```

Then inspect:

```bash
cat logs/ifvg_mtf_decision_state.json | head -c 4000
```

Expected:

```
candles > 0 on D1/H4/H1/M30/M15/M5/M1
current_price real
spread_points real
volatility_state normal/compressed/extreme
macro_state clear/blocked/mixed
sentiment_state fresh
```

---

## 4. Do not use placeholder files to “satisfy” gates

Do **not** fake these files to make the score go up:

```
data/macro/economic_calendar.json
logs/sentiment_state.json
logs/market_health.json
```

Rule:

```
Unknown macro/sentiment/spread = paper analysis allowed, live blocked.
```

---

## 5. Current hard requirements before any trade-ready signal

The system should only show paper trade ready when:

```
5/7 timeframes align
2/3 HTFs align
entry timeframe IFVG confirmed
entry displacement confirmed
liquidity sweep/displacement context present
RR to TP2 >= 2.0
spread known and acceptable (live tick)
macro clear or mixed (not stale)
sentiment non-conflicting (not stale)
daily guard clear
open position guard clear
```

If any of these are missing, verdict should remain:

```
WAIT / WATCH / WAIT_HARD_BLOCK
```

---

# What we fix next

## Patch priority 1 — local bridge-first truth

Make the full engine prefer:

```
MT5 bridge candles + spread
→ Twelve Data fallback candles only
→ CSV fallback
```

Live/paper trade-readiness must require bridge spread (live tick). Cached spread or placeholder spread should allow paper analysis only; live remains blocked.

## Patch priority 2 — macro/sentiment real sources

Use real files/API output, not placeholders:

```
data/macro/economic_calendar.json
logs/sentiment_state.json
logs/market_health.json
```

Each must include (top-level JSON):

```json
{
  "state": "clear",
  "updated_at": "2026-06-05T00:00:00+00:00",
  "source": "real_provider"
}
```

If stale (older than policy threshold), mark stale and block live readiness.

## Patch priority 3 — one UI, one runner

Keep the official local UI, the official Render dashboard, and the official local runner:

```
src/gold_trader/web/server.py
src/gold_trader/web/static/index.html
src/gold_trader/web/static/app.js
src/gold_trader/web/market_intelligence_api.py
frontend/market_intelligence/command_center.js
scripts/start.sh
scripts/ifvg_full_system_engine.py
```

All other UI entrypoints and runners become secondary/deprecated.

Render sync is handled by:

```
scripts/publish_state_to_render_payload.py
scripts/publish_state_to_render.py
POST /api/ingest-state
```

---

# Immediate command sequence (copy/paste)

Run these and inspect outputs locally:

```bash
cd ~/Gold-Trader
source .venv/bin/activate

bash scripts/stop.sh
./scripts/start.sh
```

In a second terminal:

```bash
curl -s -H "X-Gold-Bridge-Secret: <bridge-secret>" http://127.0.0.1:8765/last-tick | head -c 1000
echo
PYTHONPATH=src GOLD_BRIDGE_URL="http://127.0.0.1:8765" GOLD_BRIDGE_SECRET="<bridge-secret>" .venv/bin/python scripts/ifvg_full_system_engine.py
echo
cat logs/ifvg_mtf_decision_state.json | head -c 4000
```

That will reveal the local blockers and what to fix next.

---

Current authoritative docs:

```
docs/SYSTEM_TRUTH.md
docs/RENDER_FRONTEND_MODE.md
docs/FINAL_LOCAL_OPERATION.md
docs/PAPER_JOURNAL_AND_PERFORMANCE.md
```
