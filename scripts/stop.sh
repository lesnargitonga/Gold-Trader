#!/usr/bin/env bash
# stop.sh — bring down everything start.sh launched.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_DIR="$ROOT/.run"

stopped=0
stop_pid_file() {
    local pf="$1"
    local name="$2"
    [[ -f "$pf" ]] || return 0
    local pid
    pid="$(cat "$pf" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        echo "✓ stopped $name (pid=$pid)"
        stopped=$((stopped + 1))
    fi
    rm -f "$pf"
}

for pf in "$PID_DIR"/*.pid; do
    [[ -f "$pf" ]] || continue
    name="$(basename "$pf" .pid)"
    stop_pid_file "$pf" "$name"
done

# Bridge / MT5 may outlive the nohup shell — stop by pattern too.
for pattern in "gold_trader.live.mt5_bridge_server" "terminal64.exe" "ifvg_auto_scout" "live_trade_watch"; do
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
        # shellcheck disable=SC2086
        kill $pids 2>/dev/null || true
        echo "✓ stopped $pattern"
        stopped=$((stopped + 1))
    fi
done

if [[ $stopped -eq 0 ]]; then
    echo "Nothing to stop."
fi
