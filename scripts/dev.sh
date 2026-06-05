#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
NODE_BIN="${NODE_BIN:-node}"

if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
fi

if ! command -v "$NODE_BIN" >/dev/null 2>&1; then
  if [ -x "/Applications/Codex.app/Contents/Resources/node" ]; then
    NODE_BIN="/Applications/Codex.app/Contents/Resources/node"
  else
    echo "node is required to start the web app" >&2
    exit 1
  fi
fi

cleanup() {
  jobs -p | xargs -r kill 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$ROOT/apps/api"
# Ensure the browser operator runs LIVE (not degraded): install the Chromium
# runtime once (no-op if already present). Set SKIP_BROWSER_INSTALL=1 to opt out.
if [ "${SKIP_BROWSER_INSTALL:-0}" != "1" ]; then
  "$PYTHON_BIN" -m playwright install chromium >/dev/null 2>&1 || \
    echo "warning: 'playwright install chromium' failed; browser operator will run degraded" >&2
fi
"$PYTHON_BIN" -m uvicorn main:app --reload --port "${API_PORT:-8000}" &
"$PYTHON_BIN" -m connectors.worker_main &

cd "$ROOT/apps/web"
"$NODE_BIN" node_modules/next/dist/bin/next dev --webpack --port "${WEB_PORT:-3000}" &

wait
