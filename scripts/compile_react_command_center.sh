#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/frontend/react_command_center/app.jsx"
OUT="$ROOT/frontend/react_command_center/app.js"
npx --yes esbuild "$SRC" --loader:.jsx=jsx --format=iife --target=es2020 --outfile="$OUT"
echo "compiled $OUT"
