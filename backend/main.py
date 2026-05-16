import logging
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import get_settings
from models.database import init_database
from models.schemas import HealthResponse
from routers.chat import router as chat_router
from routers.graph import router as graph_router
from routers.jira import router as jira_router
from routers.license import router as license_router
from routers.local_models import router as local_models_router
from routers.memory import router as memory_router
from routers.preferences import router as preferences_router
from routers.sessions import router as sessions_router
from routers.settings import router as settings_router
from routers.source_import import router as source_import_router
from routers.specialists import router as specialists_router
from routers.workspace import router as workspace_router
from routers.enrichment import router as enrichment_router
from routers.mcp import router as mcp_router
from routers.retrieval_search import router as retrieval_router
from routers.connections import router as connections_router
from services.enrichment_service import start_workers, stop_workers

logger = logging.getLogger(__name__)

try:
    APP_VERSION = version("jarvis-backend")
except PackageNotFoundError:
    APP_VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db_path = settings.workspace_path / "app" / "jarvis.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    await init_database(db_path)
    # Markdown -> SQLite reindex stays synchronous: it's a single SQL truncate
    # plus a directory walk, well under a second on multi-thousand-note vaults.
    from services.memory_service import reindex_all
    count = await reindex_all()
    if count > 0:
        logger.info("Startup reindex: %d notes indexed", count)
    # Embedding reindex (fastembed CPU inference) goes to the background task
    # supervisor — see ADR 003 §I and services/reindex_supervisor.py. Frontend
    # polls /api/memory/reindex/status to surface a non-blocking toast.
    from services import reindex_supervisor
    await reindex_supervisor.start_async()
    # Seed built-in specialists for existing workspaces
    try:
        from services.specialist_service import seed_builtin_specialists
        seeded = seed_builtin_specialists()
        if seeded:
            logger.info("Seeded built-in specialists: %s", seeded)
    except Exception as exc:
        logger.debug("Specialist seeding skipped: %s", exc)
    await start_workers()
    # ML warmup: preload fastembed embedder, reranker, spaCy NER, and HF
    # tokenizers in a background thread so the first user-facing chat turn
    # doesn't pay the lazy-load cost. See services/warmup_service.py and
    # docs/features/chat.md for the full rationale (cold-start turn-2 stall).
    #
    # Defer the warmup START by 2 s. The warmup thread holds Python's GIL
    # for ~30-40 s during spaCy's `xx_ent_wiki_sm` import + first inference,
    # which starves uvicorn's asyncio loop and queues every inbound HTTP
    # request behind the load. The Tauri shell's first request after
    # `JARVIS_BACKEND_READY` is the license probe (`POST /api/license/state`),
    # which gates the splash screen → real-layout transition. Without this
    # delay the user sees a 60+ s splash even on warm-cache launches; with
    # it, the license probe and any other immediate startup requests get
    # served first (the GIL is uncontended for the first ~2 s post-yield),
    # then warmup runs in the background while the user is already in the
    # real UI. ADR 022 records the architectural reasoning.
    async def _deferred_warmup() -> None:
        import asyncio
        await asyncio.sleep(2)
        from services import warmup_service
        warmup_service.start()
    import asyncio as _asyncio
    _asyncio.create_task(_deferred_warmup())
    yield
    # Stop background pieces in reverse order of startup.
    await stop_workers()
    # First-run orchestrator (ADR 005 §B) is user-triggered, not lifespan-
    # triggered; we only need to cancel any in-flight task at shutdown.
    from services import first_run_orchestrator
    await first_run_orchestrator.cancel_and_wait()
    await reindex_supervisor.cancel_and_wait()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(title="Jarvis API", version=APP_VERSION, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Accept", "Origin"],
    )

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", version=APP_VERSION)

    @app.get("/api/health/warm")
    async def warmup_status() -> dict:
        """Snapshot of ML-component warmup progress.

        The frontend polls this on app launch to surface a "preparing models"
        affordance during the ~10-15 s background warmup. Returns the per-
        component state map (see services/warmup_service.py) plus an aggregate
        ``ready`` flag. Always 200 — readiness is a payload field, not an HTTP
        status, so the route is cheap to hit while loading.
        """
        from services import warmup_service
        snap = warmup_service.status()
        snap["ready"] = warmup_service.is_ready()
        return snap

    app.include_router(workspace_router)
    app.include_router(memory_router)
    app.include_router(chat_router)
    app.include_router(sessions_router)
    app.include_router(preferences_router)
    app.include_router(graph_router)
    app.include_router(specialists_router)
    app.include_router(settings_router)
    app.include_router(source_import_router)
    app.include_router(local_models_router)
    app.include_router(jira_router)
    app.include_router(enrichment_router)
    app.include_router(retrieval_router)
    app.include_router(connections_router)
    app.include_router(mcp_router)
    app.include_router(license_router)
    # ADR 015: no `bundle` router (no build-flag to advertise) and no
    # `api_keys` router (no cloud providers). Single-target local-only
    # build means the route surface is fixed at import time.

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("main:app", host=settings.api_host, port=settings.api_port, reload=True)
