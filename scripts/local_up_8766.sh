#!/usr/bin/env bash
# Local paper bring-up on alt bridge port 8766 (cypher keeps :8765).
# Bypasses ./start's bridge-launch (Wine 10013 flake) and uses the manual
# pseudo-TTY launch that binds reliably. Idempotent.
ROOT="/home/lesnar/Documents/Gold trader"
VENV="$ROOT/.venv/bin/python"
CRED="$HOME/.gold-mt5-wine/credentials.env"
BRIDGE_SCRIPT="$HOME/.gold-mt5-wine/start-bridge.sh"
PORT=8766
URL="http://127.0.0.1:$PORT"
mkdir -p "$ROOT/.run" "$ROOT/logs"

# 1) stop old gold procs (keep MT5 terminal + cypher)
for p in scripts/ifvg_auto_scout.py gold_trader.web.market_intelligence_api mt5_bridge_server winpy/python.exe scout_to_decision_state.py; do
  pkill -9 -f "$p" 2>/dev/null || true
done
[ -f "$ROOT/.run/decision_bridge.pid" ] && kill -9 "$(cat "$ROOT/.run/decision_bridge.pid")" 2>/dev/null || true
WPID=$(ss -ltnp 2>/dev/null | grep ':8770' | grep -o 'pid=[0-9]*' | cut -d= -f2 | head -1)
[ -n "$WPID" ] && kill -9 "$WPID" 2>/dev/null || true
rm -f "$ROOT/.run/"*.pid 2>/dev/null || true
sleep 3

# 2) pin creds to 8766 + unified secret
"$VENV" "$ROOT/scripts/_pin_creds_8766.py" || true

# 3) load creds (now 8766 + unified secret + MT5 login)
set -a
. "$CRED"
set +a
export GOLD_BRIDGE_HOST=127.0.0.1 GOLD_BRIDGE_PORT=$PORT GOLD_BRIDGE_URL="$URL"

# 4) launch bridge on 8766 if not already listening
if ! ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  setsid script -qefc "bash $(printf '%q' "$BRIDGE_SCRIPT")" "$ROOT/logs/bridge.log" >/dev/null 2>&1 &
  for i in $(seq 1 20); do
    sleep 3
    ss -ltn 2>/dev/null | grep -q ":$PORT " && break
  done
fi

# 5) scout — 1H primary zone, 5m/1m LTF entry, 60s interval
setsid env PYTHONPATH=src GOLD_BRIDGE_URL="$URL" GOLD_BRIDGE_SECRET="${GOLD_BRIDGE_SECRET:-}" \
  GOLD_SYMBOL="${GOLD_SYMBOL:-GOLD}" IFVG_SCOUT_INTERVAL=60 IFVG_SCOUT_TF=60 \
  OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
  "$VENV" "$ROOT/scripts/ifvg_auto_scout.py" >> "$ROOT/logs/ifvg_scout.process.log" 2>&1 &
echo $! > "$ROOT/.run/scout.pid"

# 6) web UI on 8770
setsid env PYTHONPATH=src GOLD_TRADER_ROOT="$ROOT" GOLD_BRIDGE_URL="$URL" GOLD_BRIDGE_SECRET="${GOLD_BRIDGE_SECRET:-}" \
  OPENAI_API_KEY="${OPENAI_API_KEY:-}" HOST=127.0.0.1 PORT=8770 \
  "$VENV" -m gold_trader.web.market_intelligence_api >> "$ROOT/logs/web.log" 2>&1 &
echo $! > "$ROOT/.run/web.pid"

# 6b) authoritative bridge: corrected scout brief -> decision state -> cloud payload (loop)
setsid bash -c '
R="/home/lesnar/Documents/Gold trader"; V="$R/.venv/bin/python"
while true; do
  PYTHONPATH=src "$V" "$R/scripts/scout_to_decision_state.py" >> "$R/logs/scout_decision_bridge.log" 2>&1 || true
  PYTHONPATH=src "$V" "$R/scripts/publish_state_to_render_payload.py" >> "$R/logs/scout_decision_bridge.log" 2>&1 || true
  sleep 20
done' >/dev/null 2>&1 &
echo $! > "$ROOT/.run/decision_bridge.pid"
sleep 6

# 7) status
echo "=== creds ==="; grep -E 'GOLD_BRIDGE_(URL|PORT)=' "$CRED"
echo "=== bridge :$PORT ==="
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo "LISTENING"
  curl -s -m5 -H "X-Gold-Bridge-Secret: ${GOLD_BRIDGE_SECRET:-}" "$URL/healthz" | head -c 240; echo
  curl -s -m6 -H "X-Gold-Bridge-Secret: ${GOLD_BRIDGE_SECRET:-}" "$URL/candles?symbol=${GOLD_SYMBOL:-GOLD}&timeframe=60&count=1" | head -c 200; echo
else
  echo "NOT LISTENING — tail logs/bridge.log:"; tail -4 "$ROOT/logs/bridge.log" | sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' | tr -d '\r'
fi
echo "=== UI ==="; curl -s -m4 -o /dev/null -w "UI http %{http_code}\n" http://127.0.0.1:8770/
echo "=== authoritative decision (from corrected scout) ==="
curl -s -m4 http://127.0.0.1:8770/api/decision | "$VENV" -c "import sys,json; d=json.load(sys.stdin); print('action=',d.get('action'),'grade=',d.get('final_grade'),'score=',d.get('final_score'),'side=',d.get('side'),'engine=',d.get('engine'),'src=',(d.get('data_health') or {}).get('data_source'),'sync=',d.get('cloud_sync'))" 2>/dev/null || echo "(decision endpoint not ready yet)"
echo "=== procs ==="
printf "bridge=%s scout=%s web=%s mt5=%s\n" \
  "$(ps -eo cmd|grep -c '[m]t5_bridge_server')" \
  "$(ps -eo cmd|grep -c '[i]fvg_auto_scout.py')" \
  "$(ps -eo cmd|grep -c '[m]arket_intelligence_api')" \
  "$(ps -eo cmd|grep -c '[t]erminal64.exe')"
