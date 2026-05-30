import importlib.util

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from core.exceptions import PermissionDenied

from jobs import context_update, profile_synthesis, scheduled_tasks
from core.db import engine, reflect_table
from runtime import task_runner
from routers import activity, approvals, artifact_share, artifacts, attachments, auth, chat, connectors, context, memory, projects, schedules, search, settings, tasks, workflows

app = FastAPI(title="Chronos API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):30\d{2}",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
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
    for scheduler in (profile_synthesis.scheduler, context_update.scheduler, scheduled_tasks.scheduler):
        if not scheduler.running:
            scheduler.start()
    task_runner.start_runner()
    await recover_incomplete_tasks()
    await recover_incomplete_workflows()


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
    """Deep health check — verifies all critical dependencies."""
    import time

    checks: dict[str, str] = {}

    # Postgres
    try:
        async with engine.begin() as conn:
            await conn.execute(select(1))  # type: ignore[arg-type]
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {exc}"

    # Redis
    try:
        from core.redis import redis_client
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    # MinIO
    try:
        from core.config import settings as cfg
        from miniopy_async import Minio  # type: ignore[import]
        client = Minio(cfg.minio_endpoint, access_key=cfg.minio_access_key,
                       secret_key=cfg.minio_secret_key, secure=cfg.minio_secure)
        await client.bucket_exists(cfg.minio_bucket)
        checks["minio"] = "ok"
    except Exception as exc:
        checks["minio"] = f"error: {exc}"

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
    except Exception as exc:
        checks["model"] = f"degraded: {exc!r:.120}"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "checks": checks, "ts": time.time()}
