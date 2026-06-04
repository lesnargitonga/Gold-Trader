#!/usr/bin/env bash
# status.sh — print the live status of every moving part.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv/bin/python"
PID_DIR="$ROOT/.run"

CRED="$HOME/.gold-mt5-wine/credentials.env"
[[ -f "$CRED" ]] && . "$CRED"
BRIDGE_URL="${GOLD_BRIDGE_URL:-http://127.0.0.1:8765}"
WEB_PORT="${GOLD_WEB_PORT:-8770}"

echo "── Gold Trader status ─────────────────────────────────"

# Web UI
if [[ -f "$PID_DIR/web.pid" ]] && kill -0 "$(cat "$PID_DIR/web.pid")" 2>/dev/null; then
    echo "web UI       : ✓ running (pid=$(cat "$PID_DIR/web.pid"), http://127.0.0.1:$WEB_PORT)"
else
    echo "web UI       : ✗ stopped"
fi

# IFVG auto-scout (always-on AI watch)
if [[ -f "$PID_DIR/scout.pid" ]] && kill -0 "$(cat "$PID_DIR/scout.pid")" 2>/dev/null; then
    echo "IFVG scout   : ✓ running (pid=$(cat "$PID_DIR/scout.pid"), logs/ifvg_scout.log)"
else
    existing_scout="$(pgrep -f "scripts/ifvg_auto_scout.py" | head -n 1 || true)"
    if [[ -n "$existing_scout" ]] && kill -0 "$existing_scout" 2>/dev/null; then
        echo "IFVG scout   : ✓ running (pid=$existing_scout, unmanaged; run ./start to capture pid)"
    else
        echo "IFVG scout   : ✗ stopped"
    fi
fi
if [[ -f "$ROOT/logs/ifvg_scout.log" ]]; then
    last_scout="$(tail -n 1 "$ROOT/logs/ifvg_scout.log" 2>/dev/null || true)"
    [[ -n "$last_scout" ]] && echo "scout line   : $last_scout"
fi
if [[ -f "$ROOT/logs/ifvg_scout_state.json" ]]; then
    scout_status="$("$VENV" -c "
import json
d = json.load(open('$ROOT/logs/ifvg_scout_state.json'))
brief = d.get('approval_brief') or {}
print(d.get('status','?'), '· M'+str(d.get('primary_timeframe',15)), '·', brief.get('headline','')[:60])
" 2>/dev/null || true)"
    [[ -n "$scout_status" ]] && echo "scout state  : $scout_status"
fi

# Bridge
if GOLD_BRIDGE_URL="$BRIDGE_URL" GOLD_BRIDGE_SECRET="${GOLD_BRIDGE_SECRET:-}" "$VENV" -c "
import os, urllib.request, sys
try:
    url = os.environ['GOLD_BRIDGE_URL'].rstrip('/') + '/healthz'
    req = urllib.request.Request(url)
    secret = os.environ.get('GOLD_BRIDGE_SECRET') or ''
    if secret:
        req.add_header('X-Gold-Bridge-Secret', secret)
    urllib.request.urlopen(req, timeout=2).read()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    echo "MT5 bridge   : ✓ online ($BRIDGE_URL)"
else
    echo "MT5 bridge   : ✗ offline ($BRIDGE_URL)"
fi

# Cron
if crontab -l 2>/dev/null | grep -q "run_agent_cycle.sh"; then
    count="$(crontab -l 2>/dev/null | grep -c "run_agent_cycle.sh" || true)"
    if [[ "$count" -gt 1 ]]; then
        echo "agent cron   : ⚠ duplicate entries ($count) — run scripts/install_cron.sh"
    else
        echo "agent cron   : ✓ installed"
    fi
else
    echo "agent cron   : · not installed (run scripts/install_cron.sh to enable)"
fi

# Champion
if [[ -f "$ROOT/config/champion.json" ]]; then
    sel="$("$VENV" -c "
import json
d = json.load(open('$ROOT/config/champion.json'))
print(d.get('selected_at',''), '·', d.get('active_families_csv') or '(none survived gates)')
" 2>/dev/null || echo unreadable)"
    echo "champion.json: ✓ $sel"
else
    echo "champion.json: · not yet generated (run scripts/weekly_champion.py)"
fi

# Last agent cycle
if [[ -f "$ROOT/logs/agent.log" ]]; then
    last="$(tac "$ROOT/logs/agent.log" 2>/dev/null | grep -m1 "agent-cycle" || true)"
    [[ -n "$last" ]] && echo "last cycle   : $last"
fi
