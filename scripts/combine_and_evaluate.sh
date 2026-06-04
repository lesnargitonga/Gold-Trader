#!/usr/bin/env bash
# combine_and_evaluate.sh
# Waits for all download processes to finish, combines the 15m CSVs into a
# full 1-year dataset, then re-runs holdout-eval on the 3 candidate strategies.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
DATA="$ROOT/data"
VENV="$ROOT/.venv/bin/python"
CLI="$VENV -m gold_trader.cli"

echo "=== combine_and_evaluate.sh ==="
echo "Root: $ROOT"

# ---------------------------------------------------------------------------
# 1. Wait for download directories to appear
# ---------------------------------------------------------------------------
EXPECTED_DIRS=(xauusd_y2 xauusd_y3 xauusd_y4 xauusd_y5)

for dir in "${EXPECTED_DIRS[@]}"; do
    target="$DATA/$dir"
    echo -n "Waiting for $dir ... "
    timeout=0
    while [[ ! -d "$target" || -z "$(ls -A "$target" 2>/dev/null)" ]]; do
        sleep 30
        (( timeout += 30 ))
        if (( timeout > 1800 )); then
            echo "TIMEOUT waiting for $dir after 30 minutes — skipping"
            break
        fi
    done
    if [[ -d "$target" && -n "$(ls -A "$target" 2>/dev/null)" ]]; then
        echo "OK ($(ls "$target" | wc -l) files)"
    fi
done

# ---------------------------------------------------------------------------
# 2. Combine all 15m CSVs into one file
# ---------------------------------------------------------------------------
echo ""
echo "=== Combining 15m CSVs ==="

COMBINED_15M="$DATA/xauusd_full_15m.csv"
INPUTS=(
    "$DATA/recent90_xauusd/xauusd_2026-02-04_2026-05-04_15m.csv"
)
for dir in xauusd_y2 xauusd_y3 xauusd_y4 xauusd_y5; do
    csv="$(ls "$DATA/$dir/"*_15m.csv 2>/dev/null | head -1)"
    if [[ -f "$csv" ]]; then
        INPUTS+=("$csv")
        echo "  Found: $csv"
    else
        echo "  Missing 15m CSV in $dir"
    fi
done

if [[ ${#INPUTS[@]} -ge 2 ]]; then
    cd "$ROOT"
    $CLI combine-csv "${INPUTS[@]}" --output "$COMBINED_15M"
    echo "Combined output: $COMBINED_15M"
else
    echo "Not enough input files — aborting"
    exit 1
fi

# ---------------------------------------------------------------------------
# 3. Run holdout evaluations
# ---------------------------------------------------------------------------
echo ""
echo "=== Holdout Evaluations (1-year dataset) ==="

FAMILIES=(asian_range_breakout ny_session_breakout momentum_burst)
RESULTS_DIR="$ROOT/results"
mkdir -p "$RESULTS_DIR"

for family in "${FAMILIES[@]}"; do
    echo ""
    echo "--- $family ---"
    result_file="$RESULTS_DIR/holdout_${family}_$(date +%Y%m%d).txt"
    cd "$ROOT"
    $CLI holdout-eval "$COMBINED_15M" \
        --family "$family" \
        --holdout-fraction 0.25 \
        --n-permutations 2000 \
        | tee "$result_file"
done

echo ""
echo "=== All done. Results saved to $RESULTS_DIR ==="
