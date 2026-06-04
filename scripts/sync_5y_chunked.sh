#!/usr/bin/env bash
# Chunked 5-year XAUUSD fetch from Dukascopy. Yearly slices for resumability.
# After all chunks succeed, concatenates per-timeframe CSVs into data/xauusd_5y/.
set -u
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
END="2026-05-04"
TFS="1,5,15,60,240,1440"
export GOLD_DUKASCOPY_SKIP_FAILED=1   # tolerate persistently-flaky individual hours
WORKERS=8
OUTDIR="data/xauusd_5y"
mkdir -p "$OUTDIR" logs

# Yearly chunks: end-date and days, working backward.
# 5y ≈ 1827 days, split into 5 chunks of 366 (overlap-safe; sync handles dedup via UTC bucketing).
CHUNKS=(
  "y1:2022-05-04:366"
  "y2:2023-05-04:366"
  "y3:2024-05-04:366"
  "y4:2025-05-04:366"
  "y5:2026-05-04:366"
)

for spec in "${CHUNKS[@]}"; do
  IFS=":" read -r tag end days <<<"$spec"
  cdir="$OUTDIR/chunk_${tag}"
  log="logs/sync_5y_${tag}.log"
  if [[ -f "$cdir/.done" ]]; then
    echo "[$tag] already complete, skipping"
    continue
  fi
  echo "[$tag] fetching $days days ending $end -> $cdir"
  mkdir -p "$cdir"
  if PYTHONPATH=src $PY -u -m gold_trader.cli sync-dukascopy \
        --symbol XAUUSD --days "$days" --end-date "$end" \
        --base-interval-minutes 1 --timeframes "$TFS" \
        --max-workers "$WORKERS" --output-dir "$cdir" \
        > "$log" 2>&1; then
    touch "$cdir/.done"
    echo "[$tag] OK ($(wc -l < "$cdir"/xauusd_*15m.csv 2>/dev/null || echo '?') 15m bars)"
  else
    echo "[$tag] FAILED — see $log"
    exit 1
  fi
done

echo "=== concatenating per-timeframe CSVs ==="
for tf in 1 5 15 60 240 1440; do
  out="$OUTDIR/xauusd_5y_${tf}m.csv"
  first=1
  for tag in y1 y2 y3 y4 y5; do
    src=$(ls "$OUTDIR/chunk_${tag}"/xauusd_*_${tf}m.csv 2>/dev/null | head -1) || true
    [[ -z "$src" ]] && continue
    if [[ $first -eq 1 ]]; then
      cp "$src" "$out"
      first=0
    else
      tail -n +2 "$src" >> "$out"
    fi
  done
  if [[ -f "$out" ]]; then
    # dedupe on timestamp (col 1) and sort
    head -1 "$out" > "$out.hdr"
    tail -n +2 "$out" | sort -u -t, -k1,1 >> "$out.hdr"
    mv "$out.hdr" "$out"
    echo "$out: $(($(wc -l < "$out") - 1)) bars"
  fi
done

echo "DONE."
