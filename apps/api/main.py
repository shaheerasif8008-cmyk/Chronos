from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

import asyncio
from jobs import context_update, profile_synthesis
from core.db import engine, reflect_table
from runtime.executor import TaskExecutor
from routers import approvals, auth, chat, connectors, context, memory, tasks

app = FastAPI(title="Chronos API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(memory.router)
app.include_router(connectors.router)
app.include_router(context.router)
app.include_router(tasks.router)
app.include_router(approvals.router)


@app.on_event("startup")
async def start_schedulers() -> None:
    for scheduler in (profile_synthesis.scheduler, context_update.scheduler):
        if not scheduler.running:
            scheduler.start()
    await recover_incomplete_tasks()


async def recover_incomplete_tasks() -> list[str]:
    tasks_table = await reflect_table("tasks")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(tasks_table.c.id).where(tasks_table.c.status.in_(["pending", "planning", "running"]))
            )
        ).all()

    task_ids = [str(row[0]) for row in rows]
    for task_id in task_ids:
        asyncio.create_task(TaskExecutor().resume(task_id))
    return task_ids


@app.on_event("shutdown")
async def stop_schedulers() -> None:
    for scheduler in (profile_synthesis.scheduler, context_update.scheduler):
        if scheduler.running:
            scheduler.shutdown(wait=False)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
