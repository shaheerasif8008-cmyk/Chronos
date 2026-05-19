from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from jobs import context_update, profile_synthesis
from routers import auth, chat, connectors, memory

app = FastAPI(title="Chronos API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(memory.router)
app.include_router(connectors.router)


@app.on_event("startup")
async def start_schedulers() -> None:
    for scheduler in (profile_synthesis.scheduler, context_update.scheduler):
        if not scheduler.running:
            scheduler.start()


@app.on_event("shutdown")
async def stop_schedulers() -> None:
    for scheduler in (profile_synthesis.scheduler, context_update.scheduler):
        if scheduler.running:
            scheduler.shutdown(wait=False)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
