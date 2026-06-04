#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
compile_one() {
  local name="$1"
  local src="$ROOT/frontend/$name/app.jsx"
  local out="$ROOT/frontend/$name/app.js"
  if [[ ! -f "$src" ]]; then
    echo "skip $name (no app.jsx)"
    return 0
  fi
  npx --yes esbuild "$src" --loader:.jsx=jsx --format=iife --target=es2020 --outfile="$out"
  echo "compiled $out"
}
compile_one react_command_center_v3
compile_one react_command_center
