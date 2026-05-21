# Chronos

Chronos is an autonomous AI agent platform for enterprises. This checkout is set up for the current Phase 1 Sprint 4 surface: OTP auth, chat-triggered task creation, activity, approvals, connector seams, memory, and governed draft workflows.

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
cd ../..
npm run dev
```

If `npm` is not available but dependencies are already installed, run `bash scripts/dev.sh`; the script starts FastAPI and the Next.js Sprint 4 surface directly.

Open `http://localhost:3000/chat`. If you are not signed in, the app redirects to `/login`. Use `admin@example.com`; the OTP prints in the API terminal.

Sprint 4 proof path: after login, send `operator workflow proof: research leads, draft outreach, and request approval`. Chronos should create a task, use deterministic fixture leads, and stop with pending drafts in `/approvals` without live search or provider keys.

## Structure

```text
apps/api     FastAPI backend, migrations, auth, chat, and core seams
apps/web     Next.js frontend for login, chat, activity, approvals, settings, memory, and connectors
context      Phase 1 local organization context folder
skills       Seed skill packs
packages     Shared TypeScript types
```

## Core Seams

- `apps/api/core/permissions.py`: `permission.check()`
- `apps/api/core/memory.py`: `memory.retrieve()`
- `apps/api/core/tool_broker.py`: `tool_broker.execute()`

These are intentionally Phase 1 stubs with audit logging. Their signatures should not change.
