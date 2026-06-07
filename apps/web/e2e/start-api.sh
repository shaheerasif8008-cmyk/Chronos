#!/usr/bin/env bash
# Boots the Chronos API for E2E against an isolated test database.
# Model/provider keys are inherited from the shell that launches Playwright
# (e.g. `set -a; source <repo>/.env; set +a` before `npx playwright test`),
# so no secrets are hard-coded here.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
API_DIR="$HERE/../../api"
cd "$API_DIR"

# shellcheck disable=SC1091
source .venv/bin/activate

# Isolated storage for the harness (override-able from the environment).
export DATABASE_URL="${E2E_DATABASE_URL:-postgresql+asyncpg://chronos:chronos@localhost:55433/chronos}"
export REDIS_URL="${E2E_REDIS_URL:-redis://localhost:6379/3}"
export OBJECT_STORAGE_BACKEND=s3
export AWS_S3_BUCKET="${E2E_AWS_S3_BUCKET:-chronos-e2e-local-fallback}"
export AWS_S3_REGION="${AWS_S3_REGION:-us-east-1}"
export VAULT_ENCRYPTION_KEY="${VAULT_ENCRYPTION_KEY:-$(printf '0%.0s' $(seq 1 64))}"

LOG_DIR="$HERE/.artifacts"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/api.log"
: > "$LOG_FILE"   # truncate previous run so OTP reads are fresh

# tee so auth.setup.ts can scrape the dev OTP that the API prints to stdout.
uvicorn main:app --port "${E2E_API_PORT:-8001}" 2>&1 | tee "$LOG_FILE"
