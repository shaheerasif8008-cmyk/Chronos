# Chronos E2E harness (Playwright)

Isolated, behavioral end-to-end tests that drive the **real** web app + API.

## What it does

- Boots its own API (`e2e/start-api.sh`, port **8001**) against an **isolated
  test database** and the web app via `next start` (port **3001**). The web
  auto-derives its API base from its own port (3001 → 8001), so the harness
  never collides with a dev instance on 3000/8000.
- Authenticates once via the real dev-OTP flow (`auth.setup.ts`) and reuses the
  session (`e2e/.auth/user.json`).

## Prerequisites

- Postgres (pgvector), Redis, MinIO running (docker-compose).
- A migrated, seeded test DB. The harness expects `DATABASE_URL` to point at it
  (default `…@localhost:55433/chronos`).
- API deps installed in `apps/api/.venv`; web deps installed (`npm install`).
- A working model key in the environment (the chat spec uses the real model).
  Export it before running, e.g. `set -a; source <repo>/.env; set +a`.

## Run

```bash
cd apps/web
set -a; source <repo>/.env; set +a            # model keys (OpenRouter/DeepSeek)
export DATABASE_URL="postgresql+asyncpg://chronos:chronos@localhost:55433/chronos"
export REDIS_URL="redis://localhost:6379/3"
npx playwright test                            # all specs
npx playwright test chat.spec.ts               # one spec
npm run test:e2e:mobile                        # Pixel 7 Chromium + iPhone 15 WebKit
```

The focused mobile command uses deterministic in-browser API fixtures, so it
does not require provider credentials or a seeded database. It still runs the
real production Next.js bundle and exercises the real responsive route code.

## Proof levels

- Files ending in `-static.spec.ts` inspect source contracts. They are useful
  regression guards, but they do not navigate a browser, call the API, or prove
  runtime behavior. Run them with `npm run test:static`.
- The specs listed below use a real browser against the isolated web/API stack,
  except `mobile-responsive.spec.ts`, which uses a real production Next.js
  bundle with deterministic in-browser API fixtures.
- Passing either class locally is not production evidence. The release gate must
  rerun the behavioral flows against the deployed Git SHA and supplement them
  with exhaustive Computer Use screenshots for desktop/mobile routes and
  states. Provider-backed Browserbase, E2B, Gmail, Stripe, SendGrid, and
  multimodal operations require their own live smoke evidence.

### Specs

| Spec | Proves |
|------|--------|
| `smoke.spec.ts` | authenticated workspace renders |
| `chat.spec.ts` | send → live model stream → durable persistence |
| `artifacts.spec.ts` | create → edit → version → diff → restore |
| `memory.spec.ts` | add → retrieve → edit → delete |
| `activity.spec.ts` | task execution timeline persists across refresh |
| `model-selection.spec.ts` | model choice persists across reload |
| `connectors.spec.ts` | directory renders catalog + reflects a connected app (seeded) |
| `approvals.spec.ts` | inbox renders a pending approval → Approve decides it |
| `mobile-responsive.spec.ts` | mobile navigation, core surfaces, auth, and onboarding stay reachable without viewport overflow in Chromium and WebKit |

### Approvals spec needs a seeded approval

`approvals.spec.ts` is deterministic: it approves a **pre-seeded** approval
rather than relying on the model to call a gated tool. Seed it first and pass
the IDs:

```bash
cd apps/api && . .venv/bin/activate
export DATABASE_URL="postgresql+asyncpg://chronos:chronos@localhost:55433/chronos"
eval "$(python seed_approval.py)"              # exports APPROVAL_ID / TASK_ID
cd ../web
E2E_SEED_APPROVAL_ID="$APPROVAL_ID" E2E_SEED_TASK_ID="$TASK_ID" \
  npx playwright test approvals.spec.ts
```

The broker approval **gate** and the **decide → resume** path are proven
separately and deterministically in
`apps/api/tests/test_approval_flow_http.py`.

### Connectors spec needs a seeded connector

`connectors.spec.ts` proves the directory reflects a connected app. The real
connect path is OAuth (not E2E-driven), so seed an active connector first:

```bash
cd apps/api && . .venv/bin/activate
export DATABASE_URL="postgresql+asyncpg://chronos:chronos@localhost:55433/chronos"
python seed_connector.py                       # seeds an active gmail connector
cd ../web && npx playwright test connectors.spec.ts
```
