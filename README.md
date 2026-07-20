# Chronos

Chronos is an enterprise AI agent platform targeting total practical parity with the combined capability set of ChatGPT, Claude.ai, and Manus.ai. The product goal is a polished platform for governed autonomous execution, persistent organizational memory, projects, artifacts, deep research, multimodal work, connectors, browser/computer operation, coding, scheduled work, collaboration, and enterprise admin.

The canonical product goal is documented in [`CHRONOS_TOTAL_PARITY_GOAL.md`](CHRONOS_TOTAL_PARITY_GOAL.md), and the controlling parity acceptance matrix is [`docs/chronos_total_parity_matrix.md`](docs/chronos_total_parity_matrix.md). The current checkout contains substantial implementations across that program, including Cognito/SSO/SCIM, governed task execution, projects and knowledge, memory, artifacts, research, connectors, Browserbase browser sessions, E2B computer/repository workspaces, workflows, collaboration, compliance export, notifications, Stripe billing, and a macOS bridge. It is still not complete parity or a production certificate; see the matrix and launch runbook for the remaining product and live-evidence blockers.

## Local Setup

```bash
cp .env.example .env
docker-compose up -d

cd apps/api
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
python seed.py
cd ../..
npm run dev
```

If `npm` is not available but dependencies are already installed, run `bash scripts/dev.sh`; the script starts FastAPI and the Next.js app directly.

Open `http://localhost:3000/chat`. If you are not signed in, the app redirects to `/login`. Use `admin@example.com`; the OTP prints in the API terminal.

Foundation proof path: after login, send `operator workflow proof: research leads, draft outreach, and request approval`. Chronos should create a task, use deterministic fixture leads, and stop with pending drafts in `/approvals` without live search or provider keys.

## Production Operations

The supported production path is Terraform-managed AWS infrastructure. Start
with the operator documentation before changing an account or routing traffic:

- [`docs/PRODUCTION_CONFIGURATION.md`](docs/PRODUCTION_CONFIGURATION.md) —
  complete external/runtime configuration inventory and launch evidence;
- [`docs/TERRAFORM_STATE_ADOPTION.md`](docs/TERRAFORM_STATE_ADOPTION.md) — safe
  state bootstrap, adoption, migration, and recovery;
- [`docs/PRODUCTION_OPERATIONS.md`](docs/PRODUCTION_OPERATIONS.md) — zero-task
  first deployment, routine releases, monitoring, incidents, and client
  onboarding; and
- [`docs/DISASTER_RECOVERY.md`](docs/DISASTER_RECOVERY.md) — backups, restores,
  regional disaster response, and rehearsal requirements.

Infrastructure declarations and tests are not live launch proof. Do not route
real clients until the launch-blocking checklist in the operations runbook has
current evidence for the exact deployed Git SHA.

### Gmail OAuth

To connect Gmail locally, create or edit a Google OAuth client and add this exact Authorized redirect URI:

```text
http://localhost:8000/connectors/gmail/oauth-callback
```

Then set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI` in `.env`. The redirect URI in Google Cloud Console must exactly match `GOOGLE_REDIRECT_URI`, including protocol, host, port, and path.

## Structure

```text
apps/api     FastAPI backend, migrations, auth, chat, and core seams
apps/web     Next.js frontend for the unified product shell and client/admin surfaces
apps/desktop-macos  Sandboxed native macOS local-computer bridge
context      Local organization context folder
skills       Seed skill packs
packages     Shared TypeScript types
```

## Core Seams

- `apps/api/core/permissions.py`: `permission.check()`
- `apps/api/core/memory.py`: `memory.retrieve()`
- `apps/api/core/tool_broker.py`: `tool_broker.execute()`

These seams are non-negotiable. Their implementations may evolve, but their role as the mandatory gateway for permissions, memory, and tools should not change.
