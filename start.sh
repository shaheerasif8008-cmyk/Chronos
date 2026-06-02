#!/usr/bin/env bash
set -e

echo "==> Starting Chronos system..."
CERT_PATH="$(cd apps/api/.venv && ../.venv/bin/python -c 'import certifi; print(certifi.where())')"
export SSL_CERT_FILE="$CERT_PATH"
export REQUESTS_CA_BUNDLE="$CERT_PATH"

echo "  SSL certs: $SSL_CERT_FILE"

echo "==> Infrastructure (Docker)..."
docker compose up -d

echo "==> Backend (FastAPI)..."
cd apps/api
../.venv/bin/alembic upgrade head
cd ../..
echo "  Starting uvicorn..."
SSL_CERT_FILE="$CERT_PATH" nohup apps/api/.venv/bin/uvicorn main:app --reload --port 8000 > /tmp/backend.log 2>&1 &
echo "  Backend PID: $!"

echo "==> Frontend (Next.js)..."
echo "  Starting next dev..."
nohup npm run dev --prefix apps/web > /tmp/frontend.log 2>&1 &
echo "  Frontend PID: $!"

echo ""
echo "Done. Backend: http://localhost:8000  Frontend: http://localhost:3000"
