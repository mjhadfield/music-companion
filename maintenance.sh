#!/usr/bin/env bash
# Launch the local maintenance UI for pulling fresh data (etl/refresh.py
# wrapped in a small web dashboard). Local only -- see etl/maintenance/server.py.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT=8643
URL="http://localhost:${PORT}/"

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

python3 etl/maintenance/server.py &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

wait_for_port "$PORT" && open_url "$URL"

wait "$SERVER_PID"
