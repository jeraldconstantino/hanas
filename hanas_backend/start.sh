#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'USAGE'
Run the HANAS FastAPI backend for local development.

Usage:
  ./start.sh

Environment overrides:
  HOST=0.0.0.0          Bind address. Use 0.0.0.0 for WSL/VPN browser access.
  PORT=8080             Backend port.
  RELOAD=true           Enable uvicorn reload. Set false for a single process.
  PYTHON_BIN=venv/bin/python
  APP_MODULE=app.main:app

Examples:
  ./start.sh
  PORT=8000 ./start.sh
  HOST=0.0.0.0 PORT=8000 RELOAD=false ./start.sh
USAGE
  exit 0
fi

PYTHON_BIN="${PYTHON_BIN:-venv/bin/python}"
APP_MODULE="${APP_MODULE:-app.main:app}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
RELOAD="${RELOAD:-true}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python environment not found at: $PYTHON_BIN" >&2
  echo "Create it first or run with PYTHON_BIN=/path/to/python ./start.sh" >&2
  exit 1
fi

args=(
  -m uvicorn "$APP_MODULE"
  --host "$HOST"
  --port "$PORT"
)

if [[ "$RELOAD" == "true" || "$RELOAD" == "1" ]]; then
  args+=(--reload)
fi

echo "Starting HANAS backend on http://$HOST:$PORT"
echo "APP_ENV=${APP_ENV:-from config/.env default} RELOAD=$RELOAD"
exec "$PYTHON_BIN" "${args[@]}"
