import importlib.util
import logging
import os
import re
import ssl
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import certifi

# macOS: ensure the default SSL context uses certifi's CA bundle, otherwise
# HTTPS requests (Cognito, OpenRouter, etc.) fail with
# CERTIFICATE_VERIFY_FAILED.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
_default_ssl_ctx = ssl.create_default_context(cafile=certifi.where())
ssl._create_default_https_context = lambda: _default_ssl_ctx

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from core.config import settings as app_settings
from core.exceptions import PermissionDenied

logger = logging.getLogger(__name__)
from core.auth import get_current_member
from core.scim import SCIMError as _SCIMError
from core.tenancy import resolve_org_id

from jobs import admin_lifecycle as admin_lifecycle_jobs
from jobs import computer_sessions as computer_session_jobs
from jobs import task_cleanup as task_cleanup_jobs
from jobs import connector_write_ledger, context_update, monitor_polling, notification_delivery, profile_synthesis, retention, scheduled_tasks
from core.db import engine, reflect_table
from core.leader import LeaderElection
from core.redis import redis_client
from runtime import task_runner
from runtime.research_executor import start_research
from routers import activity, admin, admin_lifecycle, agents, approvals, artifact_share, artifacts, attachments, auth, autonomy, billing, browser_sessions, chat, comments, compliance, computer_sessions, connectors, context, custom_integrations, data, desktop_devices, desktop_sessions, domains, file_security, memory, monitors, notifications, projects, research, schedules, scim, search, settings, skills, sso, tasks, workflows


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Own process startup/shutdown using FastAPI's supported lifespan API."""

    await start_schedulers()
    try:
        yield
    finally:
        await stop_schedulers()


app = FastAPI(
    title="Chronos API",
    version="0.1.0",
    docs_url=None if app_settings.is_production else "/docs",
    redoc_url=None if app_settings.is_production else "/redoc",
    openapi_url=None if app_settings.is_production else "/openapi.json",
    lifespan=_lifespan,
)


# Catch-all error boundary. Registered BEFORE CORSMiddleware so it sits INSIDE
# it (Starlette: first-added middleware is innermost): an unhandled exception
# becomes a JSON 500 that flows back out through CORSMiddleware and gets CORS
# headers. Without this, unhandled exceptions bypass CORS entirely and the
# browser reports an unreadable "Failed to fetch" instead of the real error.
@app.middleware("http")
async def _json_error_boundary(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception:  # noqa: BLE001 — last-resort boundary for unhandled errors
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error. The failure was logged."},
        )


_UNSAFE_BROWSER_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _allowed_browser_origin(origin: str) -> bool:
    """Return whether ``origin`` is one of Chronos's controlled web origins."""

    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return False
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return False
    normalized = origin.rstrip("/").lower()
    if normalized == app_settings.frontend_base_url.rstrip("/").lower():
        return True
    host = (parsed.hostname or "").rstrip(".").lower()
    if app_settings.is_production:
        base = app_settings.base_domain.rstrip(".").lower()
        if parsed.scheme.lower() != "https" or not base or not host.endswith(f".{base}"):
            return False
        label = host[: -(len(base) + 1)]
        return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label))
    return (
        parsed.scheme.lower() == "http"
        and host in {"localhost", "127.0.0.1"}
        and port is not None
        and 3000 <= port <= 3099
    )


@app.middleware("http")
async def _csrf_cookie_guard(request: Request, call_next):
    """Reject unsafe cookie-authenticated requests from untrusted origins.

    Bearer-token clients and unauthenticated provider webhooks do not rely on
    browser cookies and are outside this CSRF boundary. Browser mutations must
    carry an Origin matching the configured app or a controlled tenant host.
    """

    if (
        request.method.upper() in _UNSAFE_BROWSER_METHODS
        and request.cookies.get("chronos_session")
        and not request.headers.get("authorization")
    ):
        origin = request.headers.get("origin", "")
        if not _allowed_browser_origin(origin):
            logger.warning(
                "Rejected cookie-authenticated request with untrusted origin: method=%s path=%s",
                request.method,
                request.url.path,
            )
            return JSONResponse(status_code=403, content={"detail": "CSRF validation failed"})
    return await call_next(request)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
    )
    response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
    if request.url.path.startswith("/shared/"):
        # A public share URL contains a bearer credential in its path. Never
        # cache or index it, and never allow the full URL to escape as a referrer.
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    if app_settings.is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"
        )
    return response


# CORS: in production, pin to the configured frontend origin only. The broad
# localhost regex is dev-only attack surface (any local app on port 30xx could
# otherwise drive credentialed requests), so it is not registered in production.
_cors_origins = [app_settings.frontend_base_url.rstrip("/")]
_cors_kwargs: dict = {"allow_origins": _cors_origins}
if app_settings.is_production and app_settings.base_domain:
    # The same web deployment serves app.<domain> and the documented
    # <tenant>.<domain> workspaces. DNS is platform-controlled; requests still
    # authenticate with an org-bound cookie and the API cross-checks its signed
    # tenant claim against the member row.
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    _cors_kwargs["allow_origin_regex"] = (
        rf"https://{label}\.{re.escape(app_settings.base_domain.rstrip('.').lower())}"
    )
else:
    _cors_origins.extend([
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ])
    _cors_kwargs["allow_origin_regex"] = r"https?://(localhost|127\.0\.0\.1):30\d{2}"

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    **_cors_kwargs,
)


@app.middleware("http")
async def _resolve_tenant(request: Request, call_next):
    """Bind each request to its tenant. Stored on request.state for the auth
    dependency; ``None`` means the no-tenant (apex/signup) context. A resolution
    failure (e.g. DB unreachable) falls to no-tenant rather than 500ing here;
    downstream auth still fails closed because member loading also requires the DB."""
    host = request.headers.get("host", "")
    org_header = request.headers.get("x-chronos-org")
    try:
        request.state.resolved_org_id = await resolve_org_id(host, org_header)
    except Exception:
        logger.warning("tenant resolution failed; treating request as no-tenant", exc_info=True)
        request.state.resolved_org_id = None
    return await call_next(request)


app.include_router(auth.router)
app.include_router(sso.router)
app.include_router(domains.router)
app.include_router(scim.router)
app.include_router(agents.router)
app.include_router(browser_sessions.router)
app.include_router(computer_sessions.router)
app.include_router(desktop_devices.router)
app.include_router(desktop_sessions.router)
app.include_router(attachments.router)
app.include_router(chat.router)
app.include_router(memory.router)
app.include_router(search.router)
app.include_router(connectors.router)
app.include_router(custom_integrations.router)
app.include_router(context.router)
app.include_router(tasks.router)
app.include_router(projects.router)
app.include_router(activity.router)
app.include_router(approvals.router)
app.include_router(notifications.router)
app.include_router(comments.router)
app.include_router(autonomy.router)
app.include_router(artifact_share.router)
app.include_router(artifacts.router)
app.include_router(settings.router)
app.include_router(admin_lifecycle.router)
app.include_router(admin.router)
app.include_router(file_security.router)
app.include_router(compliance.router)
app.include_router(billing.router)
app.include_router(workflows.router)
app.include_router(schedules.router)
app.include_router(monitors.router)
app.include_router(research.router)
app.include_router(data.router)
app.include_router(skills.router)


def _init_observability() -> None:
    """Wire configured production observability providers.

    Observability is optional for local development, but a configured provider
    must never fail silently in production.  Keeping that distinction here
    makes packaging/configuration failures visible before the process accepts
    traffic instead of discovering them during an incident.
    """
    from core.config import settings as cfg

    def provider_failure(provider: str, exc: Exception) -> None:
        message = f"Failed to initialize configured {provider} observability"
        if cfg.is_production:
            raise RuntimeError(message) from exc
        logger.warning("%s: %s", message, exc)

    # Langfuse v4 uses LiteLLM's OpenTelemetry callback.  Set credentials before
    # registering the callback so the exporter is initialized with the correct
    # endpoint on its first request.
    langfuse_configured = bool(cfg.langfuse_public_key or cfg.langfuse_secret_key)
    if langfuse_configured:
        try:
            if not (cfg.langfuse_public_key and cfg.langfuse_secret_key):
                raise ValueError("both LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required")
            if importlib.util.find_spec("langfuse") is None:
                raise ModuleNotFoundError("the langfuse SDK is not installed")

            import litellm

            os.environ["LANGFUSE_PUBLIC_KEY"] = cfg.langfuse_public_key
            os.environ["LANGFUSE_SECRET_KEY"] = cfg.langfuse_secret_key
            os.environ["LANGFUSE_OTEL_HOST"] = cfg.langfuse_host.rstrip("/")
            # Retain LANGFUSE_HOST for direct SDK consumers and older provider
            # helpers while LiteLLM v4 reads LANGFUSE_OTEL_HOST.
            os.environ["LANGFUSE_HOST"] = cfg.langfuse_host.rstrip("/")
            callbacks = list(getattr(litellm, "callbacks", []) or [])
            if "langfuse_otel" not in callbacks:
                callbacks.append("langfuse_otel")
            litellm.callbacks = callbacks
        except Exception as exc:  # noqa: BLE001 - converted into startup policy
            provider_failure("Langfuse", exc)

    # Sentry
    if cfg.sentry_dsn:
        try:
            if importlib.util.find_spec("sentry_sdk") is None:
                raise ModuleNotFoundError("the Sentry SDK is not installed")
            import sentry_sdk  # type: ignore[import]
            from sentry_sdk.integrations.fastapi import FastApiIntegration  # type: ignore[import]
            from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration  # type: ignore[import]

            sentry_sdk.init(
                dsn=cfg.sentry_dsn,
                integrations=[FastApiIntegration(), SqlalchemyIntegration()],
                traces_sample_rate=0.1,
                environment=cfg.environment,
                send_default_pii=False,
            )
        except Exception as exc:  # noqa: BLE001 - converted into startup policy
            provider_failure("Sentry", exc)


_init_observability()


@app.exception_handler(PermissionDenied)
async def _permission_denied_handler(_request: Request, exc: PermissionDenied) -> JSONResponse:
    """Map authorization denials to HTTP 403 (enforcement raises rather than returns)."""
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(_SCIMError)
async def _scim_error_handler(_request: Request, exc: _SCIMError) -> JSONResponse:
    """Render SCIM failures in the RFC 7644 Error envelope with scim+json."""
    return JSONResponse(status_code=exc.status, content=exc.to_dict(), media_type="application/scim+json")


# Schedulers that must run on exactly one instance (cron-like jobs would
# otherwise fire once per replica). They are started PAUSED and only the elected
# leader resumes them.
_SCHEDULERS = (
    admin_lifecycle_jobs.scheduler,
    profile_synthesis.scheduler,
    context_update.scheduler,
    scheduled_tasks.scheduler,
    monitor_polling.scheduler,
    retention.scheduler,
    notification_delivery.scheduler,
    computer_session_jobs.scheduler,
    task_cleanup_jobs.scheduler,
    connector_write_ledger.scheduler,
)
_scheduler_leader: LeaderElection | None = None


async def start_schedulers() -> None:
    await _bootstrap_authz()
    await _bootstrap_skills()
    # Start paused; the leader resumes them. This makes running multiple API
    # instances safe — only the leader fires schedules.
    for scheduler in _SCHEDULERS:
        if not scheduler.running:
            scheduler.start(paused=True)
    # The task runner is lease-coordinated, so every instance can run workers
    # safely (they won't double-execute a task another instance holds a lease on).
    task_runner.start_runner()

    _log = logging.getLogger(__name__)

    async def _become_leader() -> None:
        for scheduler in _SCHEDULERS:
            if scheduler.running:
                scheduler.resume()
        # Recovery is leader-only so N instances don't each re-recover every run.
        for label, recover in (
            ("Task cancellation cleanup", task_cleanup_jobs.reap_task_cleanups),
            ("Task", recover_incomplete_tasks),
            ("Workflow", recover_incomplete_workflows),
            ("Research", recover_incomplete_research),
            ("Project source", recover_pending_project_sources),
        ):
            try:
                await recover()
            except Exception as exc:
                _log.warning("%s recovery skipped: %s", label, exc)

    def _step_down() -> None:
        for scheduler in _SCHEDULERS:
            if scheduler.running:
                scheduler.pause()

    global _scheduler_leader
    _scheduler_leader = LeaderElection(
        redis_client,
        "chronos:leader:scheduler",
        on_acquire=_become_leader,
        on_release=_step_down,
    )
    await _scheduler_leader.start()


async def _bootstrap_authz() -> None:
    """Resolve/create the OpenFGA store and model when enforcement is enabled."""
    from core import authz

    if not authz.is_enabled():
        return
    try:
        await authz.ensure_store_and_model()
    except Exception:
        # Startup must not crash if OpenFGA is briefly unavailable; checks fail
        # closed at request time until the server is reachable.
        pass


async def _bootstrap_skills() -> None:
    """Seed built-in filesystem skills into the DB so they appear in the API/UI."""
    from skills.registry import sync_filesystem_skills

    try:
        await sync_filesystem_skills()
    except Exception:
        # Startup must not crash if the DB is not yet migrated/available.
        pass


async def recover_incomplete_tasks() -> list[str]:
    tasks_table = await reflect_table("tasks")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(tasks_table.c.id).where(tasks_table.c.status.in_(["queued", "pending", "planning", "running"]))
            )
        ).all()

    from runtime import leases

    task_ids: list[str] = []
    for row in rows:
        task_id = str(row[0])
        # Don't re-enqueue a task another live worker already holds a lease on —
        # that worker is actively running it. Orphans (no live lease) are revived.
        if await leases.task_lease_held(task_id):
            continue
        await task_runner.enqueue_task(task_id)
        task_ids.append(task_id)
    return task_ids


async def recover_incomplete_research() -> list[str]:
    research_runs = await reflect_table("research_runs")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(research_runs.c.id, research_runs.c.organization_id).where(
                    research_runs.c.status.in_(["pending", "planning", "running"])
                )
            )
        ).all()

    run_ids = [str(row[0]) for row in rows]
    for row in rows:
        await start_research(str(row[0]), str(row[1]))
    return run_ids


async def _tenants_with_interrupted_workflows() -> list[str]:
    """Return every org that has a workflow run in an interrupted state.

    Recovery is per-tenant, so a hardcoded ``"default"`` silently strands every
    other tenant's interrupted runs after a restart. Enumerate the distinct
    organization_ids instead.
    """
    from connectors.framework.workflows import INTERRUPTED_RUN_STATES

    runs = await reflect_table("workflow_runs")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(runs.c.organization_id)
                .where(runs.c.status.in_(sorted(INTERRUPTED_RUN_STATES)))
                .distinct()
            )
        ).all()
    return [str(row[0]) for row in rows if row[0]]


async def recover_incomplete_workflows() -> list[str]:
    from connectors.framework.queue_factory import connector_execution_queue
    from connectors.framework.repository import DatabaseConnectorRepository
    from connectors.framework.workflows import WorkflowRuntime

    runtime = WorkflowRuntime(DatabaseConnectorRepository(), connector_execution_queue())
    recovered: list[str] = []
    for tenant_id in await _tenants_with_interrupted_workflows():
        recovered.extend(await runtime.recover_interrupted_runs(tenant_id=tenant_id))
    return recovered


async def recover_pending_project_sources() -> list[str]:
    from memory.source_indexing import recover_pending_sources

    return await recover_pending_sources()


async def stop_schedulers() -> None:
    global _scheduler_leader
    if _scheduler_leader is not None:
        await _scheduler_leader.stop()
        _scheduler_leader = None
    for scheduler in _SCHEDULERS:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    await task_runner.stop_runner()


async def _core_health_checks() -> dict[str, str]:
    """Check core infrastructure dependencies. No external/billed calls.

    Returns only ``ok``/``error`` per dependency. Exception detail (which can
    leak DSN fragments, internal hostnames, and provider error bodies) is logged
    server-side, never returned to unauthenticated callers.
    """
    import logging

    log = logging.getLogger("chronos.health")
    checks: dict[str, str] = {}

    # Postgres
    try:
        async with engine.begin() as conn:
            await conn.execute(select(1))  # type: ignore[arg-type]
        checks["postgres"] = "ok"
    except Exception:
        log.exception("health check: postgres")
        checks["postgres"] = "error"

    # Redis
    try:
        from core.redis import redis_client
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception:
        log.exception("health check: redis")
        checks["redis"] = "error"

    # Object storage.
    storage_health_name = "object_storage"
    try:
        from core.config import settings as cfg
        from core.object_storage import ensure_bucket
        storage_health_name = cfg.object_storage_health_name
        await ensure_bucket()
        checks[storage_health_name] = "ok"
    except Exception:
        log.exception("health check: object storage")
        checks[storage_health_name] = "error"

    # OpenFGA is a core authorization dependency in production. Its documented
    # /healthz endpoint tests the authorization datastore without mutating state.
    try:
        from core import authz
        from core.config import settings as cfg

        if cfg.is_production or authz.is_enabled():
            checks["openfga"] = "ok" if await authz.healthcheck() else "error"
    except Exception:
        log.exception("health check: OpenFGA")
        checks["openfga"] = "error"

    # A live Redis-backed connector worker is required for queued executions.
    # Each replica publishes an expiring unique key; stale/dead workers vanish
    # automatically without a cleanup race during rolling deployments.
    try:
        from connectors.worker_main import WORKER_HEARTBEAT_PREFIX
        from core.config import settings as cfg
        from core.redis import redis_client

        if cfg.is_production:
            found = False
            async for _key in redis_client.scan_iter(match=f"{WORKER_HEARTBEAT_PREFIX}*", count=10):
                found = True
                break
            checks["connector_worker"] = "ok" if found else "error"
    except Exception:
        log.exception("health check: connector worker heartbeat")
        checks["connector_worker"] = "error"

    return checks


@app.get("/health")
async def health() -> dict:
    """Liveness/readiness probe — core dependencies only.

    Deliberately does NOT call the model provider: a model completion costs
    money and is unauthenticated here, so probing it would let any caller drive
    billed requests and would tie readiness to a third party. Use the admin-only
    ``/health/deep`` for a model-reachability probe.
    """
    import time

    checks = await _core_health_checks()
    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks, "ts": time.time()}


@app.get("/health/live")
async def health_live() -> dict:
    """Process liveness probe.

    This endpoint deliberately avoids dependency checks. ECS should restart a
    wedged process, but it should not churn every task during a temporary RDS,
    Redis, or S3 incident.
    """
    import time

    return {"status": "ok", "ts": time.time()}


@app.get("/ready")
async def readiness() -> JSONResponse:
    """Traffic readiness probe with an honest HTTP status.

    The compatibility ``/health`` endpoint keeps its historical 200 response
    and structured degraded status for operators. ALB uses this endpoint so a
    task with unavailable core storage is removed from request routing.
    """
    import time

    checks = await _core_health_checks()
    ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ok" if ready else "degraded",
            "checks": checks,
            "ts": time.time(),
        },
    )


@app.get("/health/deep")
async def health_deep(member=Depends(get_current_member)) -> dict:
    """Admin-only deep health check — includes a billed model-reachability probe.

    Gated behind ``view_admin_console`` so the (paid) model call can only be
    triggered by an authenticated admin/owner, never an anonymous caller.
    """
    import logging
    import time

    from core import permissions

    await permissions.check(member, "view_admin_console", "health")

    log = logging.getLogger("chronos.health")
    checks = await _core_health_checks()

    # Model reachability (quick probe — no retries)
    try:
        import litellm
        from core.config import settings as cfg
        from core.llm import model_kwargs
        await litellm.acompletion(
            **model_kwargs(cfg.fast_model, messages=[{"role": "user", "content": "ping"}], stream=False),
            max_tokens=1,
        )
        checks["model"] = "ok"
    except Exception:
        log.exception("health check: model probe")
        checks["model"] = "degraded"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks, "ts": time.time()}
