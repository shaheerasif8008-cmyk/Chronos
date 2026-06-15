# CI as the Completion Gate

The roadmap's Final Completion Bar requires that every change keep the suite
green. This maps the bar to the jobs in `.github/workflows/ci.yml`, which run on
every pull request and push to `main`.

| Completion-bar requirement | Enforced by (CI job) |
|----------------------------|----------------------|
| Backend tests pass | **Backend (migrate + pytest)** — provisions `pgvector/pgvector:pg15` + `redis:7`, runs `alembic upgrade head` (also guards the migration chain on a clean DB), then `pytest -q`. |
| Security / governance tests pass | Same job — `tests/test_authz.py`, `tests/test_permissions_enforce.py`, `tests/test_tenant_isolation_http.py`, `tests/test_audit_tenant_isolation.py`, `tests/test_untrusted_content_patterns.py` run as part of `pytest`. |
| Web build passes | **Web (typecheck + build)**. |
| Playwright parity tests pass | **Web E2E (static route guards)** always; **Web E2E (behavioral)** when the gating secret is present. |
| No fake UI controls / truthful degraded modes | Static route guards assert every nav route resolves to a real screen; backend tests assert degraded paths return truthful errors (e.g. `tests/test_search_degraded.py`). |

## The one gap CI cannot self-enforce: branch protection

CI **runs** the bar but does not **block merges** on its own — that requires a
GitHub branch-protection rule on `main`, which is repository configuration, not
code in this repo. To finish standing up the gate, an admin must set on `main`:

- Require status checks to pass before merging:
  - `Backend (migrate + pytest)`
  - `Web (typecheck + build)`
  - `Web E2E (static route guards)`
- Require branches to be up to date before merging.
- (Optional) Require a pull request review before merging.

Until that rule exists, the gate is advisory. Once it exists, no change can reach
`main` without satisfying the completion bar.

## Verified locally

This bar was reproduced end-to-end in a sandbox (local Postgres 16 + pgvector +
Redis): `alembic upgrade head` then `pytest -q` → **621 passed, 15 skipped, 0
failed** (615 baseline + 6 new invitation tests). The same suite is what CI runs.
