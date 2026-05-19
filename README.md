# Chronos

Chronos is an autonomous AI agent platform for enterprises. This checkout is set up for Phase 1, Sprint 1: skeleton, local infrastructure, OTP auth, streaming chat, conversation persistence, context loading, audit logging, and the three critical seams.

## Local Setup

```bash
cp .env.example .env
docker-compose up -d

cd apps/api
python3.12 -m venv .venv  # Python 3.11 also works
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
python seed.py
uvicorn main:app --reload --port 8000
```

In another terminal:

```bash
cd apps/web
npm install
npm run dev
```

Open `http://localhost:3000/login`. Use `admin@example.com`; the OTP prints in the API terminal.

## Structure

```text
apps/api     FastAPI backend, migrations, auth, chat, and core seams
apps/web     Next.js frontend for login and chat
context      Phase 1 local organization context folder
skills       Seed skill packs
packages     Shared TypeScript types
```

## Sprint 1 Seams

- `apps/api/core/permissions.py`: `permission.check()`
- `apps/api/core/memory.py`: `memory.retrieve()`
- `apps/api/core/tool_broker.py`: `tool_broker.execute()`

These are intentionally Phase 1 stubs with audit logging. Their signatures should not change.
