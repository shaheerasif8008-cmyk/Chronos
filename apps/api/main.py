import importlib.util
import logging
import os
import ssl

import certifi

# macOS: ensure the default SSL context uses certifi's CA bundle, otherwise
# HTTPS requests (Cognito, OpenRouter, etc.) fail with
# CERTIFICATE_VERIFY_FAILED.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
_default_ssl_ctx = ssl.create_default_context(cafile=certifi.where())
ssl._create_default_https_context = lambda: _default_ssl_ctx

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from core.config import settings as app_settings
from core.exceptions import PermissionDenied

from jobs import context_update, profile_synthesis, scheduled_tasks
from core.db import engine, reflect_table
from runtime import task_runner
from runtime.research_executor import start_research
from routers import activity, agents, approvals, artifact_share, artifacts, attachments, auth, browser_sessions, chat, computer_sessions, connectors, context, data, desktop_sessions, memory, monitors, projects, research, schedules, search, settings, skills, tasks, workflows

app = FastAPI(title="Chronos API", version="0.1.0")

# CORS: in production, pin to the configured frontend origin only. The broad
# localhost regex is dev-only attack surface (any local app on port 30xx could
# otherwise drive credentialed requests), so it is not registered in production.
_cors_origins = [app_settings.frontend_base_url.rstrip("/")]
_cors_kwargs: dict = {"allow_origins": _cors_origins}
if not app_settings.is_production:
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

app.include_router(auth.router)
app.include_router(agents.router)
app.include_router(browser_sessions.router)
app.include_router(computer_sessions.router)
app.include_router(desktop_sessions.router)
app.include_router(attachments.router)
app.include_router(chat.router)
app.include_router(memory.router)
app.include_router(search.router)
app.include_router(connectors.router)
app.include_router(context.router)
app.include_router(tasks.router)
app.include_router(projects.router)
app.include_router(activity.router)
app.include_router(approvals.router)
app.include_router(artifact_share.router)
app.include_router(artifacts.router)
app.include_router(attachments.router)
app.include_router(settings.router)
app.include_router(workflows.router)
app.include_router(schedules.router)
app.include_router(monitors.router)
app.include_router(research.router)
app.include_router(data.router)
app.include_router(skills.router)


def _init_observability() -> None:
    """Wire Langfuse callback and Sentry SDK if keys are configured (Category 10)."""
    from core.config import settings as cfg

    # Langfuse — LiteLLM has a built-in Langfuse callback.
    if (
        cfg.langfuse_public_key
        and cfg.langfuse_secret_key
        and importlib.util.find_spec("langfuse") is not None
    ):
        try:
            import litellm

            litellm.success_callback = list(set(getattr(litellm, "success_callback", []) + ["langfuse"]))
            litellm.failure_callback = list(set(getattr(litellm, "failure_callback", []) + ["langfuse"]))
            import os
            os.environ.setdefault("LANGFUSE_PUBLIC_KEY", cfg.langfuse_public_key)
            os.environ.setdefault("LANGFUSE_SECRET_KEY", cfg.langfuse_secret_key)
            os.environ.setdefault("LANGFUSE_HOST", cfg.langfuse_host)
        except Exception:
            pass  # Langfuse is optional

    # Sentry
    if cfg.sentry_dsn:
        try:
            import sentry_sdk  # type: ignore[import]
            from sentry_sdk.integrations.fastapi import FastApiIntegration  # type: ignore[import]
            from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration  # type: ignore[import]

            sentry_sdk.init(
                dsn=cfg.sentry_dsn,
                integrations=[FastApiIntegration(), SqlalchemyIntegration()],
                traces_sample_rate=0.1,
                environment=getattr(cfg, "forge_env", "development"),
            )
        except Exception:
            pass  # Sentry is optional


_init_observability()


@app.exception_handler(PermissionDenied)
async def _permission_denied_handler(_request: Request, exc: PermissionDenied) -> JSONResponse:
    """Map authorization denials to HTTP 403 (enforcement raises rather than returns)."""
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.on_event("startup")
async def start_schedulers() -> None:
    await _bootstrap_authz()
    await _bootstrap_skills()
    for scheduler in (profile_synthesis.scheduler, context_update.scheduler, scheduled_tasks.scheduler):
        if not scheduler.running:
            scheduler.start()
    task_runner.start_runner()
    _log = logging.getLogger(__name__)
    try:
        await recover_incomplete_tasks()
    except Exception as exc:
        _log.warning("Task recovery skipped: %s", exc)
    try:
        await recover_incomplete_workflows()
    except Exception as exc:
        _log.warning("Workflow recovery skipped: %s", exc)
    try:
        await recover_incomplete_research()
    except Exception as exc:
        _log.warning("Research recovery skipped: %s", exc)


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

    task_ids = [str(row[0]) for row in rows]
    for task_id in task_ids:
        await task_runner.enqueue_task(task_id)
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


async def recover_incomplete_workflows() -> list[str]:
    from connectors.framework.queue_factory import connector_execution_queue
    from connectors.framework.repository import DatabaseConnectorRepository
    from connectors.framework.workflows import WorkflowRuntime

    return await WorkflowRuntime(DatabaseConnectorRepository(), connector_execution_queue()).recover_interrupted_runs(tenant_id="default")


@app.on_event("shutdown")
async def stop_schedulers() -> None:
    for scheduler in (profile_synthesis.scheduler, context_update.scheduler, scheduled_tasks.scheduler):
        if scheduler.running:
            scheduler.shutdown(wait=False)
    await task_runner.stop_runner()


@app.get("/health")
async def health() -> dict:
    """Deep health check — verifies all critical dependencies.

    Returns only ``ok``/``error``/``degraded`` per dependency. Exception detail
    (which can leak DSN fragments, internal hostnames, and provider error
    bodies) is logged server-side, never returned to unauthenticated callers.
    """
    import logging
    import time

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

    # Model reachability (quick probe — no retries)
    try:
        import litellm
        from core.config import settings as cfg
        from core.llm import model_kwargs
        probe = await litellm.acompletion(
            **model_kwargs(cfg.fast_model, messages=[{"role": "user", "content": "ping"}], stream=False),
            max_tokens=1,
        )
        checks["model"] = "ok"
    except Exception:
        log.exception("health check: model probe")
        checks["model"] = "degraded"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks, "ts": time.time()}
