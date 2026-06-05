#!/usr/bin/env bash
# start.sh — ONE command to bring Gold Trader fully live.
#
#   ./start
#
# Starts (idempotent, in order):
#   1. Loads credentials + saved secrets (config/secrets.json)
#   2. MetaTrader 5 terminal (Wine) if not already running
#   3. MT5 HTTP bridge on :8765 if not already online
#   4. Live trade watcher + web UI on :8770
#
# Stop everything: bash scripts/stop.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/logs"
PID_DIR="$ROOT/.run"
CRED="$HOME/.gold-mt5-wine/credentials.env"
SECRETS="$ROOT/config/secrets.json"
BRIDGE_SCRIPT="$HOME/.gold-mt5-wine/start-bridge.sh"
WINEPREFIX="${GOLD_MT5_PREFIX:-$HOME/.gold-mt5-wine}"
MT5_EXE="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"

mkdir -p "$LOG_DIR" "$PID_DIR"

log() { printf '✓ %s\n' "$*"; }
warn() { printf '⚠ %s\n' "$*" >&2; }
step() { printf '\n── %s ──\n' "$*"; }

# 1) Credentials + secrets ---------------------------------------------------
if [[ -f "$CRED" ]]; then
    # shellcheck disable=SC1090
    . "$CRED"
    GOLD_BRIDGE_SECRET_FROM_CRED="${GOLD_BRIDGE_SECRET:-}"
    unset GOLD_BRIDGE_SECRET
    export GOLD_BRIDGE_SECRET_FROM_CRED
    log "credentials loaded"
else
    warn "no $CRED — live broker may not start"
fi

if [[ -f "$SECRETS" ]] && [[ -x "$VENV" ]]; then
    eval "$(SECRETS="$SECRETS" "$VENV" - <<'PY'
import json, os, shlex
from pathlib import Path
p = Path(os.environ["SECRETS"])
data = json.loads(p.read_text()) if p.exists() else {}
out = []
# Bridge secret: env > secrets.json > credentials.env (must match web server + bridge)
bridge = (
    os.environ.get("GOLD_BRIDGE_SECRET", "").strip()
    or str(data.get("bridge_secret") or "").strip()
    or os.environ.get("GOLD_BRIDGE_SECRET_FROM_CRED", "").strip()
)
if bridge:
    out.append(f"export GOLD_BRIDGE_SECRET={shlex.quote(bridge)}")
if not os.environ.get("OPENAI_API_KEY") and data.get("openai_api_key"):
    out.append(f"export OPENAI_API_KEY={shlex.quote(str(data['openai_api_key']))}")
print("\n".join(out))
PY
)"
    [[ -n "${GOLD_BRIDGE_SECRET:-}" ]] && log "bridge secret resolved"
fi

BRIDGE_URL="${GOLD_BRIDGE_URL:-http://127.0.0.1:8765}"
WEB_PORT="${GOLD_WEB_PORT:-8770}"
export WINEPREFIX WINEARCH=win64 WINEDEBUG=-all

bridge_ping() {
    "$VENV" -c "
import urllib.request, sys
try:
    req = urllib.request.Request(
        '${BRIDGE_URL}/healthz',
        headers={'X-Gold-Bridge-Secret': '${GOLD_BRIDGE_SECRET:-}'},
    )
    urllib.request.urlopen(req, timeout=3)
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null
}

run_decision_refresh_once() {
    local steps=(
        "$ROOT/scripts/update_live_inputs.py"
        "$ROOT/scripts/ifvg_full_system_engine.py"
        "$ROOT/scripts/journal_decision_snapshot.py"
        "$ROOT/scripts/update_paper_signal_outcomes.py"
        "$ROOT/scripts/report_paper_performance.py"
        "$ROOT/scripts/publish_state_to_render_payload.py"
    )
    local step_path
    for step_path in "${steps[@]}"; do
        PYTHONPATH=src "$VENV" "$step_path" >> "$LOG_DIR/start_decision_refresh.log" 2>&1 \
            || warn "$(basename "$step_path") failed — check logs/start_decision_refresh.log"
    done
    if [[ -n "${GOLD_RENDER_INGEST_URL:-}" && -n "${GOLD_CLOUD_SYNC_TOKEN:-}" ]]; then
        PYTHONPATH=src "$VENV" "$ROOT/scripts/publish_state_to_render.py" >> "$LOG_DIR/start_decision_refresh.log" 2>&1 \
            || warn "publish_state_to_render.py failed — check logs/start_decision_refresh.log"
    fi
}

launch_bridge() {
    # Wine Python needs a pseudo-TTY; script provides it while setsid detaches cleanly.
    setsid script -qefc "bash $(printf '%q' "$BRIDGE_SCRIPT")" "$LOG_DIR/bridge.log" >/dev/null 2>&1 &
    echo $! > "$PID_DIR/bridge.pid"
}

# 2) MetaTrader 5 terminal -----------------------------------------------------
step "MetaTrader 5"
if pgrep -f "terminal64.exe" >/dev/null 2>&1; then
    log "MT5 already running"
else
    if [[ ! -f "$MT5_EXE" ]]; then
        warn "MT5 not found at $MT5_EXE — run scripts/setup_wine_mt5.sh first"
    else
        nohup wine "$MT5_EXE" >> "$LOG_DIR/mt5_terminal.log" 2>&1 &
        echo $! > "$PID_DIR/mt5.pid"
        log "MT5 starting (log: logs/mt5_terminal.log)"
        sleep 12
    fi
fi

# 3) MT5 bridge ----------------------------------------------------------------
step "MT5 bridge ($BRIDGE_URL)"
BRIDGE_STATUS="offline"
if bridge_ping; then
    BRIDGE_STATUS="online"
    log "bridge already online"
else
    if [[ ! -f "$BRIDGE_SCRIPT" ]]; then
        warn "missing $BRIDGE_SCRIPT"
    elif pgrep -f "gold_trader.live.mt5_bridge_server" >/dev/null 2>&1; then
        log "bridge process running — waiting for health"
    else
        launch_bridge
        log "bridge starting (log: logs/bridge.log)"
    fi
    for _ in $(seq 1 45); do
        if bridge_ping; then
            BRIDGE_STATUS="online"
            log "bridge online"
            break
        fi
        sleep 2
    done
    if [[ "$BRIDGE_STATUS" != "online" ]]; then
        warn "bridge still offline after 90s — check logs/bridge.log (is MT5 logged in?)"
    fi
fi

# 4) IFVG auto-scout (always-on AI watch) --------------------------------------
step "IFVG auto-scout"
SCOUT_PID_FILE="$PID_DIR/scout.pid"
SCOUT_STDOUT_LOG="$LOG_DIR/ifvg_scout.process.log"
existing_scout="$(pgrep -f "python.*scripts/ifvg_auto_scout.py" | head -n 1 || true)"
cd "$ROOT"
run_decision_refresh_once
if [[ -f "$SCOUT_PID_FILE" ]] && kill -0 "$(cat "$SCOUT_PID_FILE")" 2>/dev/null; then
    log "IFVG scout already running (pid=$(cat "$SCOUT_PID_FILE"))"
elif [[ -n "$existing_scout" ]] && kill -0 "$existing_scout" 2>/dev/null; then
    echo "$existing_scout" > "$SCOUT_PID_FILE"
    log "IFVG scout already running (pid=$existing_scout)"
else
    setsid env PYTHONPATH=src \
        GOLD_BRIDGE_URL="$BRIDGE_URL" \
        GOLD_BRIDGE_SECRET="${GOLD_BRIDGE_SECRET:-}" \
        OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
        GOLD_SYMBOL="${GOLD_SYMBOL:-GOLD}" \
        IFVG_SCOUT_INTERVAL="${IFVG_SCOUT_INTERVAL:-60}" \
        IFVG_SCOUT_TF="${IFVG_SCOUT_TF:-15}" \
        GOLD_RENDER_INGEST_URL="${GOLD_RENDER_INGEST_URL:-}" \
        GOLD_CLOUD_SYNC_TOKEN="${GOLD_CLOUD_SYNC_TOKEN:-}" \
        "$VENV" "$ROOT/scripts/ifvg_auto_scout.py" \
        >> "$SCOUT_STDOUT_LOG" 2>&1 &
    echo $! > "$SCOUT_PID_FILE"
    sleep 1
    if kill -0 "$(cat "$SCOUT_PID_FILE")" 2>/dev/null; then
        log "IFVG scout started (pid=$(cat "$SCOUT_PID_FILE"))"
    else
        warn "IFVG scout failed — tail $SCOUT_STDOUT_LOG"
        rm -f "$SCOUT_PID_FILE"
    fi
fi

# Legacy watcher retired — IFVG scout replaces live_trade_watch.py

# 5) Web UI --------------------------------------------------------------------
step "Web UI"
WEB_PID_FILE="$PID_DIR/web.pid"
WEB_LOG="$LOG_DIR/web.log"
web_pid=""
if [[ -f "$WEB_PID_FILE" ]]; then
    web_pid="$(cat "$WEB_PID_FILE" 2>/dev/null || true)"
fi
if [[ -n "$web_pid" ]] && kill -0 "$web_pid" 2>/dev/null; then
    if tr '\0' ' ' < "/proc/$web_pid/cmdline" 2>/dev/null | grep -q "gold_trader.web.market_intelligence_api"; then
        log "Render command center already running locally (pid=$web_pid, port=$WEB_PORT)"
    else
        warn "replacing old local web UI process with Render command center (pid=$web_pid)"
        kill "$web_pid" 2>/dev/null || true
        sleep 1
        rm -f "$WEB_PID_FILE"
    fi
fi

if [[ -f "$WEB_PID_FILE" ]] && kill -0 "$(cat "$WEB_PID_FILE")" 2>/dev/null; then
    :
else
    cd "$ROOT"
    setsid env PYTHONPATH=src \
        GOLD_TRADER_ROOT="$ROOT" \
        GOLD_BRIDGE_URL="$BRIDGE_URL" \
        GOLD_BRIDGE_SECRET="${GOLD_BRIDGE_SECRET:-}" \
        OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
        HOST=127.0.0.1 \
        PORT="$WEB_PORT" \
        "$VENV" -m gold_trader.web.market_intelligence_api \
        >> "$WEB_LOG" 2>&1 &
    echo $! > "$WEB_PID_FILE"
    sleep 1
    if kill -0 "$(cat "$WEB_PID_FILE")" 2>/dev/null; then
        log "Render command center started locally (pid=$(cat "$WEB_PID_FILE"))"
    else
        warn "web UI failed — tail $WEB_LOG"
        rm -f "$WEB_PID_FILE"
        exit 1
    fi
fi

# 6) Summary -------------------------------------------------------------------
cat <<EOF

════════════════════════════════════════════
 Gold Trader — up
════════════════════════════════════════════
 UI      : http://127.0.0.1:$WEB_PORT
 Broker  : $BRIDGE_URL ($BRIDGE_STATUS)
 MT5     : $(pgrep -f terminal64.exe >/dev/null && echo running || echo not detected)
 Stop    : bash scripts/stop.sh
EOF

if [[ "$BRIDGE_STATUS" != "online" ]]; then
    warn "Broker not live yet — ensure MT5 is logged in, then run ./start again"
    exit 1
fi
