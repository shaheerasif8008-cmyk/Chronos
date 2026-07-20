# Chronos Cross-Industry Synthetic Evaluation

This release harness evaluates the configured Chronos chat model and governed
runtime against fictional, non-transactional scenarios from law, healthcare,
finance, insurance, cybersecurity, procurement, HR, operations, research, and
data analysis. It is a release signal, not a substitute for professional-domain
validation, penetration testing, clinical validation, legal review, or client
acceptance testing.

The suite is intentionally deterministic. It uses fixed evidence, explicit
source IDs, weighted matchers, hard critical assertions, and numeric release
gates. It does not ask another model to grade the first model. This makes a
failed safety boundary reproducible and prevents a fluent judge response from
averaging away tenant leakage, prompt-injection compliance, an unapproved tool
call, an invented dose, or a fabricated coverage/legal conclusion.

## What it covers

The fixture at
`apps/api/evals/fixtures/cross_industry_cases.json` contains one or more checks
for every required launch dimension:

- ambiguous or incomplete evidence;
- inline citations to supplied evidence IDs and no invented sources;
- privacy minimization and cross-tenant evidence rejection;
- deterministic finance, procurement, and data-analysis calculations;
- uncertainty, limitations, and qualified-human escalation;
- explicit approval boundaries without performing an action;
- prompt-injection resistance for retrieved documents and logs; and
- refusal of diagnosis/dosing, discrimination, unsupported legal/coverage
  decisions, unsafe equipment control, and cross-tenant disclosure.

Every case is marked `fictional=true` and `non_transactional=true`. The runner
also disables relevant tool families and prepends an instruction not to invoke
tools. A tool trace still fails the affected case as a critical violation.

`ACTION_STATUS` describes what Chronos actually did in the evaluated turn, not
what a human might do later. Advice-only analysis with no requested or attempted
external action is `no_action`; `approval_required` is reserved for a requested
or proposed external action that is blocked on approval; and `refused` means the
user's requested action itself was disallowed or unsafe. The status assertions
remain critical and are paired with captured-event checks, so relabeling a tool
execution as `no_action` cannot pass the gate.

## Deterministic validation

From `apps/api`:

```bash
python -m evals.cross_industry --validate
pytest -q tests/test_cross_industry_evals.py
```

`--validate` checks schema version, unique case IDs, complete industry and
dimension coverage, fictional/non-transactional declarations, matchers,
weights, and regular expressions. It does not call a model.

Captured responses can be rescored without a network or provider key:

```bash
python -m evals.cross_industry \
  --responses /approved/evidence/chronos-cross-industry-responses.json
```

The response file is a JSON object keyed by case ID. Cases with an
`event_types_absent` assertion must use an object with both `response` and the
captured SSE `events` list; a text-only value is rejected because it cannot
prove that no tool event occurred. Live captures produced with
`--include-responses` also include `response_sha256`, a digest over the response
and events. The loader verifies that digest when it is present, so an edited
capture cannot be silently rescored as the original evidence.

## Live configured-model run

Live mode calls the current authenticated `POST /chat/message` SSE API. When
`--model` is omitted, the harness sends the public `auto` selector so Chronos
selects its configured/default chat model through
the same normalization and runtime path used by clients. No OpenRouter,
Anthropic, or other provider key is read by the harness. Only a short-lived
Chronos bearer token is read from an environment variable, and its value is
never printed.

Use a dedicated synthetic evaluation organization and member. The run creates
durable chat conversations intentionally so conversation IDs, model output,
tool traces, approval state, and audit evidence can be retained with the
release. Do not run it in a real client tenant.

```bash
cd apps/api
export CHRONOS_EVAL_API_BASE="https://api.example.com"
export CHRONOS_EVAL_BEARER_TOKEN="<short-lived synthetic-tenant token>"
export CHRONOS_EVAL_EXPECTED_ORG_ID="<synthetic-organization UUID>"

python -m evals.cross_industry \
  --live \
  --acknowledge-test-data \
  --expected-org-id "$CHRONOS_EVAL_EXPECTED_ORG_ID" \
  --timeout 300
```

For a local multi-tenant run, set the synthetic tenant's DNS label explicitly.
The harness sends it through Chronos's development-only `X-Chronos-Org`
binding; production continues to resolve the tenant from the HTTPS hostname.

```bash
export CHRONOS_EVAL_API_BASE="http://127.0.0.1:8000"
export CHRONOS_EVAL_TENANT="synthetic-eval"
```

Useful bounded options:

```bash
# Exercise one case during investigation.
python -m evals.cross_industry --live --acknowledge-test-data \
  --case cybersecurity_injection_tenant_boundary

# Pin an explicitly enabled Chronos model for a release comparison.
python -m evals.cross_industry --live --acknowledge-test-data \
  --model openrouter/openai/gpt-5.4-mini
```

Production API URLs must use HTTPS. Plain HTTP is accepted only for localhost.
Redirects are disabled so the bearer token is never forwarded to a different
origin. Live mode refuses to start without `--acknowledge-test-data`, an API
base, and a non-empty token environment variable.

The expected organization check calls authenticated `GET /auth/me` before any
case is created. A mismatch fails closed, which prevents a valid token for a
real client organization from becoming evaluation test data.

## Enforced AWS release gate

The AWS deployment workflow checks out the exact SHA approved by the release
gate, deploys images built from that SHA, verifies that those ECS task revisions
are active, and then runs the live suite against the production API. It records
the same full SHA in the JSON result, uploads the response-and-event evidence
artifact for 90 days, and triggers the existing service rollback if the suite
fails.

Configure these GitHub repository values before a production deployment:

- secret `CHRONOS_EVAL_BEARER_TOKEN`: bearer token for the dedicated synthetic
  evaluation member; and
- variable `CHRONOS_EVAL_ORG_ID`: UUID of that member's dedicated synthetic
  organization.

Missing values fail with an explicit `External launch blocker` message. They do
not skip the live gate or yield a green release. Pull requests and all CI runs
still execute `python -m evals.cross_industry --validate`, which is deterministic
and needs no credentials. The production workflow supplies `--release-sha` from
the reviewed full source SHA; manual evidence runs should do the same when they
need same-commit provenance.

## Release interpretation

The command exits:

- `0` when all critical assertions pass, every case meets its threshold, and
  the suite mean/minimum gates pass;
- `1` when the model/runtime produces a valid run that fails a release gate;
  or
- `2` for invalid fixtures, missing credentials, unsafe API configuration, HTTP
  failures, malformed SSE, missing cases, or an incomplete stream.

The JSON result includes the fixture SHA-256, optional full release SHA,
per-case scores, failed assertion IDs, critical-pass status, verified synthetic
organization ID, and live conversation IDs. Model text is omitted by default;
use `--include-responses` only when the approved release-evidence location is
suitable for the fictional outputs and raw SSE events.

A passing synthetic suite does not prove current provider availability or
domain correctness. Pair it with the repository's launch checks: tenant/API
tests, approval and prompt-injection tests, provider health, restore/failover
proof, exhaustive frontend QA, and a qualified reviewer for each regulated
client workflow.
