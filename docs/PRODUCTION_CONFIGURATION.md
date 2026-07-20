# Chronos Production Configuration Inventory

This document is the operator-facing inventory for the AWS production path. It
describes what must be configured, where each value belongs, how to verify it,
and which omissions intentionally block launch. It does not prove that any
external account is currently configured.

The deployable production contract is defined by:

- [`../.env.example`](../.env.example) for the application settings contract;
- [`../infra/terraform.tfvars.example`](../infra/terraform.tfvars.example) for
  AWS-managed production inputs;
- [`../infra/main.tf`](../infra/main.tf) for apply-blocking production guards;
- [`../infra/ecs.tf`](../infra/ecs.tf) and
  [`../infra/secrets.tf`](../infra/secrets.tf) for ECS injection; and
- [`../.github/workflows/deploy-aws.yml`](../.github/workflows/deploy-aws.yml)
  for release-time GitHub configuration.

Do not put real values in this document, `.env.example`,
`terraform.tfvars.example`, issues, chat transcripts, screenshots, CI output,
or shell history.

## Configuration authority

| Configuration class | Authoritative location | Change method | Evidence |
|---|---|---|---|
| AWS resources | Terraform in `infra/` | Reviewed plan and apply | Saved plan summary and Terraform outputs |
| Runtime secrets | AWS Secrets Manager, created from sensitive Terraform inputs | Approved secret rotation followed by a new task revision | Secret ARN/version metadata only; never the value |
| Public runtime values | ECS task definition from Terraform | Reviewed Terraform change | Registered task-definition revision |
| Web build API URL | GitHub Actions repository variable | Repository settings UI | Build summary and deployed asset behavior |
| Deployment AWS identity | GitHub Actions OIDC role | Terraform plus GitHub repository secret | Successful OIDC assumption; no static AWS keys |
| Cognito | Cognito console/API plus Terraform identifiers | Approved identity change | Hosted-login proof and token issuer/JWKS validation |
| DNS and certificates | DNS provider and ACM | Provider console with change record | Issued certificate and resolved HTTPS hostnames |
| SaaS/provider accounts | Provider console | Provider UI; secret copied once into the approved secret input | Provider-side status plus an application smoke test |
| Tenant/business settings | Chronos owner/admin UI | Product UI | Audit event and tenant-scoped readback |

## Production invariants

Every production apply must preserve all of these conditions:

- `ENVIRONMENT=production`, `AUTH_PROVIDER=cognito`, and `DEMO_MODE=false`.
- `PERMISSIONS_ENFORCE=true` with the private OpenFGA service and a strong
  pre-shared key.
- `ENFORCE_ORG_BOUND_TOKENS=true`; a legacy-token grace window is temporary,
  documented, and no longer than the active access-token lifetime.
- `ACCESS_TOKEN_EXPIRE_MINUTES` is between 5 and 1440 (24 hours); the production
  default is 60 minutes.
- Credential-bearing provider origins are fail-closed: `OPENROUTER_API_BASE`
  is exactly `https://openrouter.ai/api/v1`; `LANGFUSE_HOST` is an HTTPS DNS
  origin with no credentials, port, path, query, or fragment; and
  `COGNITO_DOMAIN` is either a lowercase AWS hosted-UI prefix or an HTTPS DNS
  origin for a custom domain with the same authority restrictions.
- A strong `JWT_SECRET` and a 64-hex-character `VAULT_ENCRYPTION_KEY`.
- TLS for the public web/API edges, RDS, and Redis.
- Real S3 with no custom endpoint and task-role credentials instead of static
  S3 access keys.
- Finite per-organization token budgets and at least two API, web, worker, and
  OpenFGA tasks outside bootstrap mode.
- Immutable API/web image tags. The deploy workflow uses the first eight
  characters of the source Git SHA, never publishes `latest`, and Terraform
  creates immutable primary and backup-Region repositories with replication.
- WAF; multi-Region CloudTrail with S3 data events and immutable evidence;
  AWS Config, GuardDuty, Security Hub, Inspector, and Access Analyzer; monitored
  alarms; a confirmed operations route; a monthly cost budget; 35-day primary
  recovery; cross-Region copies; and restore testing enabled.
- Email, error/LLM observability, and the complete billing tuple configured if
  production is sold with those capabilities. Terraform currently treats all
  of them as mandatory for a production apply.

The `terraform_data.production_guard` resource enforces the infrastructure
subset of these conditions. Application startup separately rejects the default
JWT secret, an absent/insecure vault key, unsafe credential destinations, an
out-of-range access-token lifetime, and any production auth mode that enables
development OTP. Local development retains HTTP/custom endpoint flexibility;
these destination and token-lifetime gates apply to production startup and the
Terraform production path.

## External account checklist

No row is complete until the evidence column can be produced without revealing
a secret.

| System | Required configuration | Verification evidence |
|---|---|---|
| AWS account | Billing contacts, MFA-protected break-glass access, least-privilege operator roles, CloudTrail/account security baseline | Account/role IDs and approved access review |
| ACM in primary Region | Issued wildcard certificate for `*.cognisiatech.com` and an issued certificate covering `api.cognisiatech.com` | Both certificate statuses are `ISSUED`; ARNs match Terraform inputs |
| DNS | `app`, `api`, and wildcard tenant records point to the corresponding ALBs only after healthy service proof | Public DNS resolution and HTTPS request from outside AWS |
| Cognito | Production user pool/client, callback and logout URLs, verified email delivery, password policy, account recovery, and MFA policy approved for the client tier | Login/logout/recovery/MFA test with a non-owner test user |
| GitHub | OIDC deploy-role secret and required repository variables | A workflow assumes the expected role and passes its public configuration gate |
| OpenRouter | Paid account, model access, spend cap, alerts, and production API key | Authenticated admin deep-health model check and a real chat turn |
| E2B | Paid plan/quota sized above the reviewed aggregate code/data/computer/repo concurrency, production API key; hardened deny-egress execution template; separate Xvfb/XFCE/scrot/xdotool desktop template; separate Git/Python/pytest repo template; exact computer/repo domain allowlists; TTL, snapshot, screen-size, and member/org/workspace quotas | Provider-side quota evidence plus code/data/document smoke, successful pre-use deny and allowlist attestations with live allowed/blocked captures, cloud-computer screenshot/input/pause-resume-expiry proof, and repo clone/edit/test/restart-resume proof |
| GitHub OAuth App | Direct OAuth app with callback `https://api.cognisiatech.com/connectors/github/oauth-callback` and `repo`, `read:user`, `read:org` scopes | Two members connect distinct accounts; a private repo snapshot imports without a token appearing in E2B, logs, or task output |
| Composio | Production project/key, callback URL, per-member entity scope, and enabled app auth configs | Two distinct members connect separate accounts; revocation removes access |
| Canva Connect | Dedicated production OAuth client, secret, scopes, and `https://api.cognisiatech.com/connectors/canva/oauth-callback` | Connect, create from prose/template, export, member isolation, and revoke proof |
| Browserbase | Production key/project, remote operator enabled, approved Region/session timeout, allowlisted callback/origin settings where applicable, concurrency/quota and spend alerts | Live search plus create/navigate/type/download/takeover/hand-back/restart/revoke and quota/error-state proof |
| SendGrid | Authenticated sending domain, verified sender, production key, suppression/bounce handling | Approval/task notification arrives and links to the correct tenant |
| Langfuse | Production project, keys, retention/access policy, and no sensitive prompt policy violations | A real trace is visible with expected redaction and tenant metadata |
| Sentry | Existing production project/DSN reused by FastAPI and Next.js, alert ownership, release/environment tagging, and data-scrubbing rules | Deliberate API, Next.js server-render, and browser-render errors reach the existing project without cookies, authorization/CSRF headers, request bodies, or secrets |
| Stripe | Live secret, webhook signing secret, live Pro/Enterprise price IDs, tax/refund/support ownership | Checkout plus signed webhook changes exactly one tenant plan |
| Operations route | Monitored 24x7 mailbox or paging bridge and confirmed SNS subscription | Subscription is `Confirmed`; a test alarm is received and acknowledged |
| AWS Backup | Primary/cross-Region vaults, vault locks, recovery plan, restore-test plan | Successful recovery points and a successful restore-test run |

## GitHub repository configuration

The deploy workflow refuses to build when required public values are absent.

| Type | Name | Required value |
|---|---|---|
| Secret | `AWS_DEPLOY_ROLE_ARN` | Terraform output `github_deploy_role_arn` for the current stack |
| Secret | `OPENROUTER_API_KEY` | Used by the behavioral E2E production gate on `main` |
| Variable | `AWS_REGION` | Primary AWS Region; defaults to `us-east-1` |
| Variable | `NEXT_PUBLIC_API_BASE_URL` | `https://api.cognisiatech.com` |
| Variable | `NEXT_PUBLIC_WEB_BASE_URL` | `https://app.cognisiatech.com` |
| Variable | `NEXT_PUBLIC_TERMS_URL` | Published, owner-approved HTTPS terms URL |
| Variable | `NEXT_PUBLIC_PRIVACY_URL` | Published, owner-approved HTTPS privacy URL |
| Variable | `NEXT_PUBLIC_SUPPORT_URL` | Monitored HTTPS support destination |
| Variable | `NEXT_PUBLIC_STATUS_URL` | Operational HTTPS status page |
| Variable | `PRODUCTION_SMOKE_TENANT` | Lowercase DNS label of a dedicated smoke tenant |
| Variable | `PRODUCTION_TENANT_WEB_URL` | HTTPS URL for that tenant, such as `https://smoke.cognisiatech.com` |

The deploy role trust policy must name the exact GitHub organization and
repository. The role also needs the narrowly scoped ECR scan-configuration,
start, and findings permissions declared in `infra/iam.tf`; without them the
image-vulnerability release gate fails before migrations or service updates.
Do not replace OIDC with long-lived AWS access keys.

## Terraform secret inputs

These values enter Terraform as sensitive variables and are stored in encrypted
remote state. Terraform writes them to Secrets Manager and ECS reads them at
task startup. Restrict state access as if it were production credential access.

| Terraform input | Runtime setting | Launch status |
|---|---|---|
| `jwt_secret` | `JWT_SECRET` | Hard-required; at least 32 characters |
| `vault_encryption_key` | `VAULT_ENCRYPTION_KEY` | Hard-required; exactly 64 hexadecimal characters |
| `openrouter_api_key` | `OPENROUTER_API_KEY` | Hard-required |
| `e2b_api_key` | `E2B_API_KEY` | Hard-required |
| `e2b_template_id` | `E2B_TEMPLATE_ID` | Hard-required; dedicated deny-egress execution image for code/data/document tools |
| `e2b_computer_template_id` | `E2B_COMPUTER_TEMPLATE_ID` | Hard-required; dedicated Linux desktop image with Xvfb, XFCE, scrot, and xdotool |
| `e2b_repo_template_id` | `E2B_REPO_TEMPLATE_ID` | Hard-required; dedicated image with git, Python, and pytest |
| `github_client_id` / `github_client_secret` | Direct GitHub OAuth App | Both hard-required for private repository import; credentials are Secrets Manager references |
| `composio_api_key` | `COMPOSIO_API_KEY` | Hard-required |
| `canva_client_id` / `canva_client_secret` | Dedicated Canva Connect OAuth | Both hard-required by production guard |
| `browserbase_api_key` | `BROWSERBASE_API_KEY` | Hard-required for browser search and the remote browser operator |
| `browserbase_project_id` | `BROWSERBASE_PROJECT_ID` | Hard-required; project owning encrypted Contexts and remote sessions |
| `browserbase_region` | `BROWSERBASE_REGION` | Browserbase region, default `us-east-1` |
| `browserbase_session_timeout_seconds` | `BROWSERBASE_SESSION_TIMEOUT_SECONDS` | Reconnectable session lifetime, 60-21600 seconds |
| `sendgrid_api_key` | `SENDGRID_API_KEY` | Hard-required by production guard |
| `langfuse_public_key` / `langfuse_secret_key` | Matching Langfuse settings | Both hard-required by production guard |
| `sentry_dsn` | `SENTRY_DSN` | Hard-required by production guard |
| `stripe_secret_key` / `stripe_webhook_secret` | Matching Stripe settings | Both hard-required by production guard |
| `backup_api_key` / `backup_model` | `BACKUP_API_KEY` / `BACKUP_MODEL` | Hard-required independent direct-provider fallback; production rejects `openrouter/*` backup models |
| `tavily_api_key` | `TAVILY_API_KEY` | Optional alternative live-search provider |
| `google_client_id` / `google_client_secret` | Direct Google OAuth | Optional only when Composio is the production connector path |
| `slack_client_id` / `slack_client_secret` / `slack_signing_secret` | Direct Slack OAuth and signed agent-publication ingress | Required before Slack agent publication is enabled for clients; optional to the generic production apply |
| `microsoft_client_id` / `microsoft_client_secret` / `teams_bot_app_id` | Microsoft OAuth and Teams bot identity | Required before Teams agent publication is enabled for clients; optional to the generic production apply |
| `sendgrid_inbound_public_key` | `SENDGRID_INBOUND_PUBLIC_KEY` | Required before inbound email agent publication is enabled for clients; optional to the generic production apply |
| `cognito_app_client_secret` | Cognito confidential-client secret | Required only when the selected app client has one |

Database passwords, Redis auth, and the OpenFGA API token are generated by
Terraform. `DATABASE_URL`, `REDIS_URL`, and the OpenFGA datastore URL are also
created by Terraform from the provisioned endpoints. Do not manually recreate
or print these values. `infra/post-apply.sh` checks only that each connection
secret has one `AWSCURRENT` version.

## Multimodal provider configuration and launch proof

Terraform's production guard cannot prove that an advertised feature works.
Before calling total product launch complete, record real proof for each enabled
capability.

| Capability | Required settings | Honest behavior if absent |
|---|---|---|
| Vision/OCR | `OPENROUTER_API_KEY`; `vision_model=openrouter/openai/gpt-4o-mini` | OCR/vision operations are unavailable |
| Image generation/full-image editing | `OPENROUTER_API_KEY`; `image_model=openrouter/google/gemini-3.1-flash-image` | Image operations are unavailable |
| Speech-to-text | `OPENROUTER_API_KEY`; `stt_model=openrouter/openai/gpt-4o-mini-transcribe` | Transcription is unavailable |
| Text-to-speech | `OPENROUTER_API_KEY`; `tts_model=openrouter/x-ai/grok-voice-tts-1.0` | Speech generation is unavailable |
| Live web search | Browserbase and/or Tavily credentials | Search reports a degraded/unavailable provider state |
| SaaS connectors | Composio app auth configs or direct OAuth credentials | Each unconfigured connector stays disconnected |
| Billing | Stripe secret, webhook secret, and both live price IDs | Paid checkout must remain visibly disabled |
| Email notifications | SendGrid key and verified from-address | In-app notifications can continue; email is unavailable |
| LLM fallback | Direct Anthropic `BACKUP_API_KEY`; `BACKUP_MODEL=anthropic/claude-sonnet-5` | Production refuses to boot or apply without a separate provider failure domain |

Repository tests do not make these integrations live. The release record must
link provider-side evidence for the exact production accounts and deployed SHA.
At minimum:

- Gmail: connect two different members, search/read, create a draft, approve an
  exact send payload, deliver it once, retry the same task, and show the stored
  draft/message/idempotency evidence without exposing recipient or body in the
  delivery record;
- Browserbase: create a consented session, navigate/type/download, restart an
  API task and reconnect, request live takeover, hand back, expire/revoke the
  session, and confirm the Browserbase Context was deleted;
- E2B computer: obtain real desktop pixels, send bounded input, pause/resume the
  same provider sandbox across replicas, enforce member/org quotas, export an
  artifact, and destroy it at consent expiry;
- E2B repository: private clone/import, branch/edit/test/diff/commit, restart
  recovery from S3, and the approved provider push/PR path without placing a
  GitHub token in the sandbox or logs;
- SendGrid: deliver an actionable notification from the verified domain, retry
  a controlled failure, and verify the durable receipt/dead-letter state and
  weekly-digest deduplication; and
- Stripe: create a tenant-bound checkout and portal session, process signed
  create/update/delete events exactly once, reject an invalid signature and
  cross-tenant customer rebinding, and verify the resulting plan in Chronos.

Until those checks pass, the UI must keep each unconfigured capability disabled
or visibly degraded. Do not infer provider readiness from a non-empty secret.

The `openrouter/` model prefix selects Chronos' direct, dedicated OpenRouter
contracts. Image generation and full-image reference editing use
`POST /api/v1/images`; transcription uses the JSON/base64
`POST /api/v1/audio/transcriptions` contract; and speech uses
`POST /api/v1/audio/speech` and stores the returned MP3 bytes. Model identifiers
without the `openrouter/` prefix retain the existing LiteLLM provider path.

OpenRouter's dedicated Images API does not expose mask semantics. Chronos fails
a masked edit before making a provider request; it never drops a mask and
silently performs a full-image edit. Full-image edits, variations, and
prompt-directed background changes are supported. The configured Gemini image
endpoint currently advertises one output per request, so a Chronos count of 2-4
is executed as bounded sequential one-image requests. Chronos accepts bounded
PNG, JPEG, WebP, or GIF provider responses, validates their decoded dimensions,
and normalizes them to PNG before persistence so the artifact MIME type remains
truthful.

The Grok Voice production default documents the voices Eve, Ara, Rex, Sal, and
Leo. Chronos maps its historical default `alloy` to Eve and deterministically
maps the other common OpenAI-style voice names; an unknown voice fails instead
of silently changing the requested speaker. Provider health verifies the shared
OpenRouter key only through the non-generating `GET /api/v1/key` endpoint. That
credential check is not a substitute for a controlled, billed smoke test of
each enabled capability before client launch.

The current ECS/Terraform interface injects Composio, dedicated Canva Connect,
and direct Google OAuth, but it does not inject every other direct OAuth client
listed in `.env.example`. Production should therefore use Composio for the SaaS
apps it manages, the dedicated path for Canva, and direct Google only when that
fallback is intentionally selected. Do not configure a provider console and
then claim the connector is live when its credentials cannot reach the task.

## Complete runtime setting disposition

Every assignment in `.env.example` belongs to one of the groups below. This
appendix prevents a locally available setting from being mistaken for a value
that reaches production ECS.

### Explicit non-secret ECS values

Terraform injects these values into the API, connector worker, and migration
task where applicable:

```text
ENVIRONMENT ORG_ID REGION BASE_DOMAIN
AUTH_PROVIDER FRONTEND_BASE_URL OAUTH_CALLBACK_BASE_URL
TERMS_URL PRIVACY_URL SUPPORT_URL STATUS_URL ARTIFACT_SHARE_TTL_HOURS
COMPOSIO_CALLBACK_BASE_URL COMPOSIO_ENTITY_SCOPE GOOGLE_REDIRECT_URI
BROWSERBASE_OPERATOR_ENABLED BROWSERBASE_PROJECT_ID BROWSERBASE_REGION
BROWSERBASE_SESSION_TIMEOUT_SECONDS
COGNITO_REGION COGNITO_USER_POOL_ID COGNITO_APP_CLIENT_ID COGNITO_DOMAIN
COGNITO_ISSUER_URL COGNITO_JWKS_URL COGNITO_CALLBACK_URL
COGNITO_AUTO_PROVISION_MEMBERS SSO_ENDPOINT_HOST_ALLOWLIST
ACCESS_TOKEN_EXPIRE_MINUTES ENFORCE_ORG_BOUND_TOKENS
DB_SSL_MODE OBJECT_STORAGE_BACKEND AWS_S3_BUCKET AWS_S3_REGION AWS_S3_ENDPOINT
OPENFGA_API_URL PERMISSIONS_ENFORCE DEMO_MODE
TASK_RUNNER_MAX_CONCURRENCY TASK_RUNNER_MAX_ATTEMPTS TASK_RUNNER_TIMEOUT_SECONDS
E2B_TEMPLATE_ID E2B_SANDBOX_TIMEOUT_SECONDS E2B_ALLOW_INTERNET_ACCESS
E2B_COMPUTER_ALLOW_INTERNET_ACCESS E2B_COMPUTER_EGRESS_ALLOWLIST
E2B_COMPUTER_TEMPLATE_ID
E2B_COMPUTER_IDLE_TIMEOUT_SECONDS E2B_COMPUTER_MAX_SESSION_SECONDS
E2B_COMPUTER_MAX_ACTIVE_PER_MEMBER E2B_COMPUTER_MAX_ACTIVE_PER_ORG
E2B_COMPUTER_SCREEN_WIDTH E2B_COMPUTER_SCREEN_HEIGHT
E2B_REPO_ENABLED E2B_REPO_TEMPLATE_ID E2B_REPO_ALLOW_INTERNET_ACCESS
E2B_REPO_EGRESS_ALLOWLIST
E2B_REPO_TIMEOUT_SECONDS E2B_REPO_COMMAND_TIMEOUT_SECONDS
E2B_REPO_MAX_SNAPSHOT_BYTES E2B_REPO_MAX_WORKSPACES_PER_ORG
E2B_REPO_MAX_WORKSPACES_PER_TASK
MALWARE_SCAN_REQUIRED CLAMAV_HOST CLAMAV_PORT CLAMAV_TIMEOUT_SECONDS
CLAMAV_MAX_BYTES CLAMAV_MAX_SIGNATURE_AGE_HOURS
OPENROUTER_MODEL AGENT_MODEL FAST_MODEL BACKUP_MODEL OPENROUTER_API_BASE
EMBEDDING_MODEL EMBEDDING_DIMENSIONS VISION_MODEL IMAGE_MODEL STT_MODEL TTS_MODEL
PER_ORG_DAILY_TOKEN_LIMIT STRIPE_PRICE_PRO STRIPE_PRICE_ENTERPRISE
NOTIFICATION_FROM_EMAIL LANGFUSE_HOST
```

File ingress is a hard production boundary. Terraform fixes
`MALWARE_SCAN_REQUIRED=true`, points `CLAMAV_HOST` at the loopback-only sidecar,
and reserves enough task memory for the pinned `clamav_image`. Do not expose
port 3310 through a load balancer or public security group. The API waits for a
healthy daemon, onboarding blocks when scanner health is unavailable, and
attachment/browser and connector-synchronized binary bytes are rejected on an
infected or indeterminate verdict. Active documents are also inspected for
executables, macro/embedded/external Office content, active/encrypted PDF
features, active markup, and unsafe archives. Clean connector binaries are
re-created as text-only artifacts; original provider bytes are not persisted.
Operators review metadata-only evidence at Admin > File quarantine; review and
false-positive decisions never restore blocked bytes.
The scan path also rejects a daemon whose signature publication timestamp is
older than `CLAMAV_MAX_SIGNATURE_AGE_HOURS` (48 hours in Terraform), preventing
a technically live but stale engine from satisfying readiness.
Before launch, retain evidence for a clean fixture, the standard EICAR test
fixture, a stopped-scanner failure, and current signature/version output. Never
use a live malware sample.

Network-enabled E2B profiles are deny-by-default. Set
`E2B_REPO_EGRESS_ALLOWLIST` to the exact Git/package domains needed by the repo
template. Set `E2B_COMPUTER_EGRESS_ALLOWLIST` to the organization ceiling; every
computer session must then obtain human consent for an exact-domain subset.
Chronos rejects URLs, IPs, localhost, ports, and domains outside that ceiling.
Before accepting user data, each new network-enabled sandbox must pass the
in-sandbox control probe: one configured TLS destination is reachable and
unlisted `1.1.1.1:443` is blocked. A failed or unverifiable probe destroys the
sandbox.

`AWS_S3_ENDPOINT` is deliberately blank in AWS. `OPENFGA_API_URL` is the private
service-discovery address. `ENFORCE_ORG_BOUND_TOKENS` is hard-set true.

The Next.js application reuses the API's `SENTRY_DSN`; this deployment does not
create or require another Sentry project. ECS injects the DSN as a runtime
secret for server/edge capture. After AWS OIDC authentication, the deploy reads
that existing Secrets Manager value and passes it to the web build as a masked
BuildKit secret, never a build argument or log value. Next.js embeds the public
client transport value required by the browser SDK and adds only its ingest
origin to CSP. Client/server/edge SDKs disable default PII, strip request
bodies, cookies, Authorization/Cookie/CSRF headers, tag immutable release and
environment, and sample five percent of production traces. Session Replay,
query strings and console breadcrumbs are also discarded. Session Replay, user
feedback capture, and source-map upload remain disabled; enabling any of
them requires a privacy/security review and, for source maps, a CI-only Sentry
auth token.

### Web build settings

The deploy workflow validates and passes these public values into the immutable
Next.js build. Local development discovers the same contract in `.env.example`:

```text
NEXT_PUBLIC_API_BASE_URL NEXT_PUBLIC_WEB_BASE_URL
NEXT_PUBLIC_TERMS_URL NEXT_PUBLIC_PRIVACY_URL
NEXT_PUBLIC_SUPPORT_URL NEXT_PUBLIC_STATUS_URL
NEXT_PUBLIC_CHRONOS_ENVIRONMENT NEXT_PUBLIC_CHRONOS_RELEASE
NEXT_PUBLIC_SENTRY_DSN
```

Production sources the API/web/legal/support/status values from GitHub Actions
repository variables. The release and environment are workflow-owned. The
Sentry browser transport is carried by the masked BuildKit secret described
above, not by a repository variable.

### Secrets Manager values

Terraform injects these settings as Secrets Manager references, not plaintext
task-definition environment values:

```text
ADMIN_EMAIL DATABASE_URL REDIS_URL JWT_SECRET VAULT_ENCRYPTION_KEY
OPENROUTER_API_KEY BACKUP_API_KEY E2B_API_KEY COMPOSIO_API_KEY
GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET
CANVA_CLIENT_ID CANVA_CLIENT_SECRET BROWSERBASE_API_KEY TAVILY_API_KEY
GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET COGNITO_APP_CLIENT_SECRET
SLACK_CLIENT_ID SLACK_CLIENT_SECRET SLACK_SIGNING_SECRET
MICROSOFT_CLIENT_ID MICROSOFT_CLIENT_SECRET TEAMS_BOT_APP_ID
SENDGRID_INBOUND_PUBLIC_KEY
OPENFGA_API_TOKEN SENDGRID_API_KEY LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY
SENTRY_DSN STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET
```

`DATABASE_URL`, `REDIS_URL`, and `OPENFGA_API_TOKEN` are computed/generated by
Terraform. Optional secret resources exist only when a non-empty value is
configured.

### Deliberately omitted or automatically resolved

```text
AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
OPENFGA_STORE_ID OPENFGA_MODEL_ID ORG_BOUND_TOKENS_GRACE_UNTIL
```

ECS uses its IAM task role instead of static S3 credentials. OpenFGA store/model
IDs are resolved or created through the private authorized service. The legacy
org-token grace window is blank for immediate enforcement; introducing a grace
window requires a separate time-bounded rollout change.

### Application defaults not exposed as Terraform production inputs

```text
DB_POOL_SIZE DB_MAX_OVERFLOW DB_POOL_RECYCLE
LOCAL_LLM_BASE_URL LOCAL_LLM_MODEL LOCAL_LLM_TIMEOUT_SECONDS
MEMORY_RETRIEVE_TIMEOUT_SECONDS
TASK_LEASE_TTL_SECONDS TASK_LEASE_HEARTBEAT_SECONDS TASK_REAPER_INTERVAL_SECONDS
MONITOR_POLL_INTERVAL_SECONDS MONITOR_MIN_INTERVAL_SECONDS
MONITOR_MAX_INTERVAL_SECONDS MONITOR_MAX_PER_ORG
MONITOR_MAX_RUNS_PER_ORG_CYCLE MONITOR_FETCH_TIMEOUT_SECONDS
MONITOR_FETCH_MAX_BYTES MONITOR_LEASE_SECONDS
ARTIFACT_PREVIEW_MAX_BYTES ARTIFACT_PREVIEW_MAX_UNCOMPRESSED_BYTES
ARTIFACT_PREVIEW_MAX_PDF_PAGES PROJECT_EXPORT_MAX_BYTES
PROJECT_EXPORT_MAX_ARTIFACTS
RUNTIME_AUTO_INSTALL_TOOLS RUNTIME_TOOL_INSTALL_TIMEOUT_SECONDS
BROWSERBASE_SEARCH_URL
TEAMS_BOT_JWKS_URL TEAMS_BOT_ISSUER
CONCURRENT_SUB_AGENTS AGENT_COGNITION_ENABLED AGENT_MAX_REFLECTIONS
AGENT_MAX_REPLANS MAX_CONTEXT_TOKENS RESPONSE_RESERVE_TOKENS
```

Production currently uses the validated application defaults for these values;
the local LLM path is not the production model path. A client-specific change
must add a Terraform variable and explicit ECS injection rather than relying on
an untracked task-definition edit.

### Catalog credentials not injected by the current AWS module

```text
NOTION_CLIENT_ID NOTION_CLIENT_SECRET
LINEAR_CLIENT_ID LINEAR_CLIENT_SECRET
HUBSPOT_CLIENT_ID HUBSPOT_CLIENT_SECRET AIRTABLE_CLIENT_ID AIRTABLE_CLIENT_SECRET
JIRA_CLIENT_ID JIRA_CLIENT_SECRET
SALESFORCE_CLIENT_ID SALESFORCE_CLIENT_SECRET
STRIPE_CLIENT_ID STRIPE_CLIENT_SECRET
WEBHOOK_SIGNING_KEY WEBHOOK_SIGNING_SECRET
CUSTOM_HTTP_API_BASE CUSTOM_HTTP_API_KEY REMOTE_MCP_URL REMOTE_MCP_TOKEN
```

The listed SaaS apps use Composio in the intended production path. GitHub,
Slack agent publication, and Microsoft/Teams agent publication are intentional
exceptions with direct credentials injected through Secrets Manager. GitHub's
direct OAuth client is injected because private
repo import fetches an archive inside the API using the member-scoped vault
token, then uploads only repository bytes to E2B. The token is never placed in
the sandbox, a Git URL, logs, task metadata, or S3. Generic
webhook, custom HTTP, and remote MCP connectors remain unavailable until their
credentials and egress/security policy are added to Terraform. Billing's
`STRIPE_SECRET_KEY` is separate from the optional Stripe Connect OAuth client.

### Stripe billing

Create recurring Stripe Prices for the Chronos `pro` and `enterprise` plans and
set their IDs in `STRIPE_PRICE_PRO` and `STRIPE_PRICE_ENTERPRISE`. Configure the
Stripe-hosted customer portal for subscription management, then register this
public webhook endpoint:

```text
https://api.cognisiatech.com/billing/webhook
```

The endpoint must receive `checkout.session.completed`,
`customer.subscription.created`, `customer.subscription.updated`, and
`customer.subscription.deleted`. Store that endpoint's signing secret in
`STRIPE_WEBHOOK_SECRET`; it is not the Stripe CLI signing secret. All four
Stripe values must be configured together and the two Price IDs must be
distinct. Chronos verifies the raw payload signature, maps entitlements only
from those configured Price IDs, persists the organization/customer binding,
and deduplicates webhook event IDs before changing a plan.

Checkout and portal endpoints accept the standard `Idempotency-Key` request
header. Existing clients that omit it receive a short tenant-bound server-side
deduplication window.

## Complete Terraform input disposition

The example tfvars file is the value-level source of truth. Inputs are grouped
here so every production control has an owner:

- **Platform/Region:** `aws_region`, `availability_zones`, `environment`,
  `app_name`.
- **Public edge:** `domain_name`, `web_domain_name`, `api_domain_name`,
  `web_acm_certificate_arn`, `api_acm_certificate_arn`, `terms_url`,
  `privacy_url`, `support_url`, `status_url`, `artifact_share_ttl_hours`.
- **Identity:** `auth_provider`, `cognito_region`, `cognito_user_pool_id`,
  `cognito_app_client_id`, `cognito_domain`, `cognito_issuer_url`,
  `cognito_jwks_url`, `sso_endpoint_host_allowlist`,
  `cognito_app_client_secret`, `admin_email`.
- **GitHub OIDC:** `github_org`, `github_repo`,
  `github_oidc_provider_arn`.
- **Application/provider secrets:** `jwt_secret`, `vault_encryption_key`,
  `sendgrid_api_key`, `openrouter_api_key`, `backup_api_key`, `tavily_api_key`,
  `langfuse_public_key`, `langfuse_secret_key`, `sentry_dsn`, `e2b_api_key`,
  `e2b_template_id`, `e2b_computer_template_id`, `e2b_repo_template_id`, `github_client_id`,
  `github_client_secret`, `composio_api_key`, `canva_client_id`,
  `canva_client_secret`, `browserbase_api_key`, `stripe_secret_key`,
  `stripe_webhook_secret`, `stripe_price_pro`, `stripe_price_enterprise`,
  `notification_from_email`, `slack_client_id`, `slack_client_secret`,
  `slack_signing_secret`, `microsoft_client_id`, `microsoft_client_secret`,
  `teams_bot_app_id`, `sendgrid_inbound_public_key`.
- **Runtime policy:** `demo_mode`, `permissions_enforce`,
  `per_org_daily_token_limit`, `e2b_sandbox_timeout_seconds`,
  `e2b_computer_allow_internet_access`, `e2b_computer_egress_allowlist`,
  `e2b_computer_idle_timeout_seconds`,
  `e2b_computer_max_session_seconds`, `e2b_computer_max_active_per_member`,
  `e2b_computer_max_active_per_org`, `e2b_computer_screen_width`,
  `e2b_computer_screen_height`,
  `browserbase_project_id`, `browserbase_region`,
  `browserbase_session_timeout_seconds`,
  `e2b_repo_enabled`, `e2b_repo_allow_internet_access`,
  `e2b_repo_egress_allowlist`,
  `e2b_repo_timeout_seconds`, `e2b_repo_command_timeout_seconds`,
  `e2b_repo_max_snapshot_bytes`, `e2b_repo_max_workspaces_per_org`,
  `e2b_repo_max_workspaces_per_task`.
- **API capacity:** `api_cpu`, `api_memory`, `api_desired_count`,
  `api_min_count`, `api_max_count`, `clamav_image`. `api_memory` must remain at
  least 3072 MiB because every API task includes the essential ClamAV sidecar.
- **Web capacity:** `web_desired_count`, `web_min_count`, `web_max_count`.
- **Worker/OpenFGA:** `worker_cpu`, `worker_memory`, `worker_desired_count`,
  `worker_min_count`, `worker_max_count`, `openfga_desired_count`,
  `openfga_image`.
- **Release bootstrap:** `platform_bootstrap_mode`, `api_image_tag`,
  `web_image_tag`.
- **Databases:** `db_instance_class`, `db_multi_az`, `db_engine_version`,
  `db_backup_retention_days`, `openfga_db_instance_class`,
  `openfga_db_allocated_storage`, `openfga_db_multi_az`.
- **Redis:** `redis_node_type`, `redis_num_cache_clusters`,
  `redis_automatic_failover_enabled`, `redis_multi_az_enabled`,
  `redis_snapshot_retention_days`.
- **Edge protection/logs:** `waf_enabled`, `waf_api_rate_limit`,
  `waf_auth_rate_limit`, `waf_web_rate_limit`,
  `application_log_retention_days`, `web_log_retention_days`,
  `waf_log_retention_days`, `account_security_services_enabled`,
  `audit_log_retention_days`, `cloudtrail_s3_data_events_enabled`,
  `cloudtrail_insights_enabled`.
- **Operations/recovery:** `operations_alarm_email`,
  `monthly_cost_budget_usd`, `backups_enabled`, `backup_copy_region`,
  `backup_pitr_retention_days`, `backup_copy_retention_days`,
  `automated_restore_testing_enabled`. The `restore_rehearsal_mode`,
  `restore_app_db_snapshot_identifier`,
  `restore_openfga_db_snapshot_identifier`, `restore_redis_snapshot_name`, and
  `restore_rehearsal_ingress_cidrs` inputs are deliberately blank/false in the
  production tfvars. They are used only by the separate-state workflow in
  [`DISASTER_RECOVERY.md`](DISASTER_RECOVERY.md).

Some variables exist in `variables.tf` for legacy or lower-level overrides but
are not in `terraform.tfvars.example` and/or are not injected into ECS. Do not
assume defining such a value changes production; verify the full variable to
task-definition path first.

## Implemented controls awaiting live proof

- Terraform now declares immutable primary and backup-Region ECR repositories,
  lifecycle policies, and cross-Region replication; the deploy workflow pushes
  only the Git-SHA tag. Before GA, record a rejected tag-overwrite attempt and
  matching image digests in both Regions for the released SHA.
- Terraform now tracks ECS desired counts while ignoring only task-definition
  revisions managed by the deploy workflow. This makes the bootstrap
  zero-to-baseline transition deterministic in configuration; the first live
  apply must still prove all four services reach their required counts.
- Terraform now declares a KMS-encrypted, versioned, COMPLIANCE-locked
  CloudTrail bucket and a separate KMS-encrypted/versioned AWS Config delivery
  bucket (AWS Config rejects buckets with default Object Lock retention); a
  validated multi-Region CloudTrail with artifact-object data events and
  CloudWatch security alarms; AWS Config recording; GuardDuty; Security Hub
  with AWS foundational, CIS v5, and AI security standards; Inspector; and an
  account IAM Access Analyzer. High/critical GuardDuty, Security Hub, and
  Inspector findings plus AWS Config delivery failures route to the encrypted
  operations topic. Before GA, prove every service is enabled, the trail is
  logging and validating, Config is recording, the operations route receives a
  controlled security alarm, and findings have an accountable owner.
- Dedicated Canva Connect credentials are production-gated, stored in Secrets
  Manager, and injected into ECS. A real OAuth/connect/create/export/revoke flow
  is still required before calling the connector live.
- Slack, Microsoft/Teams, and SendGrid inbound publication credentials are
  stored in Secrets Manager and injected into ECS when configured. The generic
  production apply does not require them, so total-product launch still needs
  an explicit configuration check plus live signed inbound/outbound/revoke
  evidence for each publication channel.
- Generic webhook, custom HTTP, remote MCP, and other non-Composio direct OAuth
  credentials are not available in production ECS as described above.
- A full backup-Region promotion and application restore has no recorded proof;
  see `DISASTER_RECOVERY.md`.

## Identity and tenant configuration

- The public app and tenant subdomains use the same web ALB and wildcard
  certificate; the API remains on the exact API hostname.
- Cognito callback URL is `${web_origin}/login/callback`.
- Composio and generic OAuth callback bases are the public API origin.
- Google direct OAuth callback is
  `${api_origin}/connectors/gmail/oauth-callback`.
- Production login requires a tenant context. Validate the apex, the dedicated
  smoke tenant, and at least one representative client tenant.
- Keep `COGNITO_AUTO_PROVISION_MEMBERS=false` unless the organization's
  invitation/domain policy explicitly permits automatic membership.
- Configure SSO/SCIM per tenant through the owner/admin UI and prove a negative
  case: a user outside the allowed tenant cannot authenticate or access data.
- Before admitting client data, configure and save that organization's active
  memory, deleted-memory, and deleted-artifact retention periods in **Settings
  → Memory**. The default policy is not a substitute for contractual approval.
- Record an audited retention dry run and review the candidate, pinned, and
  legal-hold exclusion counts. Add an organization-wide hold before import when
  preservation is required; do not depend on an operational note outside the
  product because only persisted `retention_holds` block the executor.

## Secret rotation

1. Open a tracked change naming the secret, owner, reason, and rollback path.
2. Create the replacement in the provider console without exposing it in logs
   or screenshots.
3. Update the approved sensitive Terraform input and review a redacted plan.
4. Apply, verify the new Secrets Manager version metadata, and deploy a new ECS
   task revision.
5. Run the capability-specific smoke test.
6. Revoke the old provider credential only after the new tasks are stable.
7. Record completion and the next rotation date without recording the value.

Rotating `VAULT_ENCRYPTION_KEY` is a data migration, not an ordinary secret
replacement: existing encrypted connector credentials must be re-encrypted or
reconnected before the old key is retired. Rotating database or Redis auth also
requires a coordinated datastore credential change; changing only the secret
value will break the application.

## Launch evidence record

Use one record per environment and release:

```text
Environment:
Git SHA / image tag:
Terraform plan identifier:
Terraform apply identifier:
DNS/certificate verification time:
Cognito login/recovery/MFA evidence:
Provider smoke evidence:
Observability test evidence:
Billing/email test evidence:
Backup recovery-point evidence:
Restore-test evidence:
Retention policy / dry-run evidence:
Behavioral E2E run:
Frontend browser/device audit:
Approvers:
Known exceptions and expiry:
```

An empty field is not an implicit pass. It is an unresolved launch item.
