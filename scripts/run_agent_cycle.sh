#!/usr/bin/env bash
# run_agent_cycle.sh
# Designed to be run via cron every 15-30 minutes.
# Syncs latest XAUUSD data and runs one agent-cycle iteration.
#
# Live trading: GOLD_BROKER=mt5_remote only when runtime_config auto_trade_enabled=true.
# Paper-only:   default while auto_trade_enabled is false.
#
# Example cron entry (every 15 minutes):
#   */15 * * * * /home/lesnar/Documents/Gold\ trader/scripts/run_agent_cycle.sh >> /home/lesnar/Documents/Gold\ trader/logs/agent.log 2>&1
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv/bin/python"
LOG="$ROOT/logs/agent.log"
CRED="$HOME/.gold-mt5-wine/credentials.env"

# Cron has a stripped env — source MT5 credentials if available.
if [[ -f "$CRED" ]]; then
    # shellcheck disable=SC1090
    . "$CRED"
fi

# runtime_config.json is the operator safety switch.  The cycle may still read
# live data while paper-only, but it must not place broker orders unless enabled.
AUTO_TRADE_ENABLED="$("$VENV" -c "import json, pathlib; \
p=pathlib.Path('$ROOT/config/runtime_config.json'); \
d=json.load(open(p)) if p.exists() else {}; \
print('true' if d.get('auto_trade_enabled') is True else 'false')" 2>/dev/null || echo false)"
export GOLD_AUTO_TRADE_ENABLED="$AUTO_TRADE_ENABLED"

if [[ "$AUTO_TRADE_ENABLED" != "true" ]]; then
    export GOLD_BROKER=paper
elif [[ -z "${GOLD_BROKER:-}" ]]; then
    if [[ -n "${GOLD_BRIDGE_SECRET:-}" ]]; then
        export GOLD_BROKER=mt5_remote
    else
        export GOLD_BROKER=paper
    fi
fi

# Phase 4: macro decision filter mode is operator-toggleable from the
# web UI (config/runtime_config.json).  Web UI write -> next cron run
# picks it up.  Falls back to "soft" if config missing or malformed.
# Explicit env var overrides the config (useful for one-off cron entries).
if [[ -z "${GOLD_MACRO_FILTER:-}" ]]; then
    if [[ -f "$ROOT/config/runtime_config.json" ]]; then
        GOLD_MACRO_FILTER="$("$VENV" -c "import json,sys; \
d=json.load(open('$ROOT/config/runtime_config.json')); \
m=d.get('macro_filter_mode','soft'); \
print(m if m in ('off','soft','hard') else 'soft')" 2>/dev/null || echo soft)"
    else
        GOLD_MACRO_FILTER="soft"
    fi
    export GOLD_MACRO_FILTER
fi

# Same pattern for news blackout window (minutes).  0 = disabled.
if [[ -z "${GOLD_NEWS_BLACKOUT_MIN:-}" ]]; then
    if [[ -f "$ROOT/config/runtime_config.json" ]]; then
        GOLD_NEWS_BLACKOUT_MIN="$("$VENV" -c "import json; \
d=json.load(open('$ROOT/config/runtime_config.json')); \
v=d.get('news_blackout_min',0); \
print(float(v) if isinstance(v,(int,float)) and 0<=float(v)<=240 else 0)" 2>/dev/null || echo 0)"
    else
        GOLD_NEWS_BLACKOUT_MIN="0"
    fi
    export GOLD_NEWS_BLACKOUT_MIN
fi

# Weekly champion selector output (see scripts/weekly_champion.py).  When
# present we trade only the families it picked; otherwise fall back to the
# proven defaults below.
#
# Operator decision: lock the agent to IFVG scout-only by default.
# This change enforces the operator's request to focus on the
# `inversion_fair_value_gap` family (IFVG) for agent-cycle runs.
DEFAULT_FAMILIES="inversion_fair_value_gap"
OPERATOR_CONTEXT_FAMILIES="inversion_fair_value_gap"
ACTIVE_FAMILIES="$DEFAULT_FAMILIES"
CHAMPION_JSON="$ROOT/config/champion.json"
if [[ -f "$CHAMPION_JSON" ]]; then
    CAND="$("$VENV" -c "import json; \
d=json.load(open('$CHAMPION_JSON')); \
print((d.get('active_families_csv') or '').strip())" 2>/dev/null || echo "")"
    if [[ -n "$CAND" ]]; then
        if [[ ",$CAND," == *",$DEFAULT_FAMILIES,"* ]]; then
            ACTIVE_FAMILIES="$CAND"
        else
            ACTIVE_FAMILIES="$DEFAULT_FAMILIES,$CAND"
        fi
    fi
fi
if [[ -n "$OPERATOR_CONTEXT_FAMILIES" ]]; then
    IFS=',' read -ra _ctx_families <<< "$OPERATOR_CONTEXT_FAMILIES"
    for family in "${_ctx_families[@]}"; do
        [[ -z "$family" ]] && continue
        if [[ ",$ACTIVE_FAMILIES," != *",$family,"* ]]; then
            ACTIVE_FAMILIES="${ACTIVE_FAMILIES:+$ACTIVE_FAMILIES,}$family"
        fi
    done
fi
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [agent-cycle] active_families=$ACTIVE_FAMILIES" | tee -a "$LOG"

# Enforce explicit IFVG-only lock to prevent other families being included
# (ignore champion.json or other overrides for now while we focus the
# system on IFVG scouting / paper/manual workflow).
ACTIVE_FAMILIES="inversion_fair_value_gap"
echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [agent-cycle] LOCKING active_families=$ACTIVE_FAMILIES (IFVG-only)" | tee -a "$LOG"

# Per-broker output dir so live and paper don't share state.
case "$GOLD_BROKER" in
    paper)                 OUTPUT_DIR="$ROOT/data/agent_live_xauusd" ;;
    mt5_remote|mt5_local)  OUTPUT_DIR="$ROOT/data/live_xauusd" ;;
    *)                     OUTPUT_DIR="$ROOT/data/agent_${GOLD_BROKER}" ;;
esac

mkdir -p "$(dirname "$LOG")" "$OUTPUT_DIR"

echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [agent-cycle] starting broker=$GOLD_BROKER auto_trade_enabled=$AUTO_TRADE_ENABLED" | tee -a "$LOG"

"$VENV" -m gold_trader.cli agent-cycle \
    --symbol XAUUSD \
    --days 21 \
    --base-interval-minutes 5 \
    --timeframes "5,15,60,240" \
    --max-workers 4 \
    --output-dir "$OUTPUT_DIR" \
    --families "$ACTIVE_FAMILIES" \
    --iterations 1 \
    --max-candidates 6 \
    --risk-per-trade 0.01 \
    --max-daily-trades 2 \
    --paper-equity 10000 \
    2>&1 | tee -a "$LOG"

# Phase 4: append any newly-closed paper trades to the journal
# (regime tags + filter verdict + execution drift). Idempotent.
# Failure here must NOT break the cron pipeline.
set +e
"$VENV" "$ROOT/scripts/update_journal.py" \
    --paper-state "$OUTPUT_DIR/paper_state.json" \
    --journal "$ROOT/logs/trade_journal.csv" \
    --bars "$OUTPUT_DIR/xauusd_15m.csv" \
    --macro-dir "$ROOT/data/macro" \
    2>&1 | tee -a "$LOG"
set -e

echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [agent-cycle] done" | tee -a "$LOG"
"$VENV" "$ROOT/scripts/mtf_paper_signal.py" --data-dir "$OUTPUT_DIR" --symbol xauusd 2>&1 | tee -a "$LOG"
# Phase 14b: macro paper-signal (validated PREMIUM construct on 60m + 240m).
# Standalone like the MTF script. Failure must not break the pipeline.
set +e
"$VENV" "$ROOT/scripts/macro_paper_signal.py" --data-dir "$OUTPUT_DIR" --symbol xauusd --tf 60m 2>&1 | tee -a "$LOG"
"$VENV" "$ROOT/scripts/macro_paper_signal.py" --data-dir "$OUTPUT_DIR" --symbol xauusd --tf 240m 2>&1 | tee -a "$LOG"
set -e
