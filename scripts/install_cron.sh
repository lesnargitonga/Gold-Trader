#!/usr/bin/env bash
# install_cron.sh — installs the two scheduled jobs.
#   - agent-cycle every 15 minutes (live trading loop)
#   - weekly champion at 22:00 UTC every Sunday
# Idempotent: removes old entries with the same comment markers before adding.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

MARK_AGENT="# gold-trader:agent-cycle"
MARK_CHAMP="# gold-trader:champion"

# Strip any existing entries with our markers
existing="$(crontab -l 2>/dev/null || true)"
filtered="$(echo "$existing" | grep -v "$MARK_AGENT" | grep -v "$MARK_CHAMP" | grep -v "run_agent_cycle.sh" | grep -v "weekly_champion.py" || true)"

NEW="$filtered
$MARK_AGENT
*/15 * * * * cd $(printf '%q' "$ROOT") && bash scripts/run_agent_cycle.sh
$MARK_CHAMP
0 22 * * 0 cd $(printf '%q' "$ROOT") && PYTHONPATH=src .venv/bin/python scripts/weekly_champion.py --days 30 >> logs/champion.log 2>&1
"

echo "$NEW" | crontab -
echo "✓ cron jobs installed:"
crontab -l | grep -E "$MARK_AGENT|$MARK_CHAMP" -A1
