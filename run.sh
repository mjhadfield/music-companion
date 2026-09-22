#!/usr/bin/env bash
# Build the public database and serve the frontend locally.
#
# Usage:
#   ./run.sh          # serves on http://localhost:8642
#   PORT=3000 ./run.sh # or pick a different port
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT="${PORT:-8642}"
URL="http://localhost:${PORT}/"

if [ ! -f data/music.sqlite ]; then
  echo "data/music.sqlite not found -- run the ETL scripts first (see README's Getting Started)." >&2
  exit 1
fi

wait_for_port() {
  local port="$1" tries=50
  while (( tries-- > 0 )); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

open_url() {
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$1" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then
    open "$1" >/dev/null 2>&1 &
  fi
}

echo "Building public database (site/public/music.sqlite)..."
python3 etl/build_public_db.py

echo ""
echo "Serving site/ at ${URL} (Ctrl+C to stop)"
cd site
python3 -m http.server "$PORT" &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

wait_for_port "$PORT" && open_url "$URL"

wait "$SERVER_PID"
