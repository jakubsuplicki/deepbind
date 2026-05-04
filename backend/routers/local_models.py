"""Local models router — hardware probe, runtime status, model catalog & pull."""

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.chat_model_probe import (
    PROBE_CONFIG_KEY,
    current_environment,
    iter_probe_events,
    needs_rerun,
    persist_probe_result,
    read_probe_result,
    set_user_override,
)
from services.chat_model_probe import ProbeEvidence, ProbeResult
from services import first_run_orchestrator
from services.ollama_service import (
    HardwareProfile,
    ModelRecommendation,
    PullRequest,
    RuntimeLoad,
    RuntimeStatus,
    SelectRequest,
    TestRequest,
    TestResponse,
    WarmUpRequest,
    build_catalog,
    clear_active_local_model,
    delete_model,
    get_active_local_model,
    list_installed_models,
    probe_hardware,
    probe_runtime,
    probe_runtime_load,
    pull_model_stream,
    set_active_local_model,
    test_model,
    warm_up_model,
    DEFAULT_OLLAMA_BASE_URL,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/local", tags=["local-models"])


def _config_path():
    """Path to ``app/config.json`` in the active workspace.

    Resolves ``get_settings`` lazily so test fixtures that monkey-patch
    ``config.get_settings`` (the convention used elsewhere in this router's
    test suite) take effect on each call.
    """
    from config import get_settings
    return get_settings().workspace_path / "app" / "config.json"


@router.get("/hardware", response_model=HardwareProfile)
async def get_hardware() -> HardwareProfile:
    """Detect local hardware profile (RAM, disk, CPU, GPU)."""
    return probe_hardware()


@router.get("/runtime", response_model=RuntimeStatus)
async def get_runtime(
    base_url: str = Query(DEFAULT_OLLAMA_BASE_URL, alias="base_url"),
) -> RuntimeStatus:
    """Check if Ollama is installed and running."""
    return await probe_runtime(base_url)


@router.get("/runtime/load", response_model=RuntimeLoad)
async def get_runtime_load(
    base_url: str = Query(DEFAULT_OLLAMA_BASE_URL, alias="base_url"),
) -> RuntimeLoad:
    """Snapshot the current runtime load (RAM/swap/GPU + Ollama-loaded models).

    Pure-Python today (psutil + Ollama /api/ps). Consumed by the future
    memory-pressure auto-downgrade — when free RAM drops below the headroom
    needed for the loaded model + KV cache, swap to a smaller model that fits
    rather than letting Ollama OOM or thrash swap. macOS branch graduates to
    a Tauri-side native helper after ADR 003 lands.
    """
    return await probe_runtime_load(base_url)


@router.get("/models/catalog")
async def get_catalog(
    base_url: str = Query(DEFAULT_OLLAMA_BASE_URL, alias="base_url"),
) -> list:
    """Return model catalog with hardware-based recommendations."""
    hw = probe_hardware()
    active = get_active_local_model()
    active_id = active.get("model_id") if active else None
    catalog = await build_catalog(hw, base_url, active_model_id=active_id)
    return [r.model_dump() for r in catalog]


@router.get("/models/installed")
async def get_installed(
    base_url: str = Query(DEFAULT_OLLAMA_BASE_URL, alias="base_url"),
) -> list:
    """List models currently downloaded in Ollama."""
    return await list_installed_models(base_url)


@router.post("/models/pull")
async def pull_model(req: PullRequest):
    """Pull (download) a model from Ollama. Streams progress via SSE."""
    return StreamingResponse(
        pull_model_stream(req.model, req.base_url),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/models/select")
async def select_model(req: SelectRequest):
    """Set the active local model."""
    set_active_local_model(req.model_id, req.ollama_model, req.base_url)
    return {"status": "ok", "active_model": req.model_id}


@router.delete("/models/{model_name:path}")
async def remove_model(
    model_name: str,
    base_url: str = Query(DEFAULT_OLLAMA_BASE_URL, alias="base_url"),
):
    """Delete a model from Ollama."""
    success = await delete_model(model_name, base_url)
    if success:
        # If this was the active model, clear it from config
        active = get_active_local_model()
        if active and active.get("ollama_model", "") == model_name:
            clear_active_local_model()
        return {"status": "ok", "deleted": model_name}
    return {"status": "error", "message": "Failed to delete model"}


@router.post("/models/test", response_model=TestResponse)
async def test_model_endpoint(req: TestRequest) -> TestResponse:
    """Quick test that a model works — returns latency and speed."""
    return await test_model(req.model, req.base_url)


@router.post("/models/warm-up")
async def warm_up(req: WarmUpRequest):
    """Send a tiny prompt to keep model loaded in Ollama memory."""
    success = await warm_up_model(req.model, req.base_url)
    return {"status": "warm" if success else "failed"}


# ── Chat-model self-test (ADR 012) ─────────────────────────────────────────


class ChatModelProbeOverrideRequest(BaseModel):
    """Set or clear ``user_override`` on the persisted probe record."""

    model: Optional[str] = None  # None clears the override


@router.get("/chat-model-probe")
async def get_chat_model_probe(
    base_url: str = Query(DEFAULT_OLLAMA_BASE_URL, alias="base_url"),
):
    """Read the persisted probe verdict + whether a re-run is required.

    The frontend calls this on app boot and on the settings page. When
    ``needs_rerun`` is true (Ollama version bump, OS major bump, new
    catalog model), the UI prompts the user to re-test. ``persisted`` is
    None on first launch — the UI then triggers ``/run`` automatically.
    """
    persisted = read_probe_result(_config_path())
    runtime = await probe_runtime(base_url)
    env = current_environment(ollama_version=runtime.version)
    rerun, reason = needs_rerun(persisted, env)
    return {
        "persisted": persisted,
        "needs_rerun": rerun,
        "rerun_reason": reason,
        "current_environment": {
            "ollama_version": env.ollama_version,
            "platform": env.platform,
            "catalog_models": list(env.catalog_models),
        },
        "runtime_reachable": runtime.reachable,
    }


@router.post("/chat-model-probe/run")
async def run_chat_model_probe(
    base_url: str = Query(DEFAULT_OLLAMA_BASE_URL, alias="base_url"),
):
    """Stream probe progress as SSE; persist the result on the ``complete`` event.

    Refuses to start if Ollama isn't reachable — running the probes
    against a down runtime would just fail every candidate with
    ``fail_unreachable`` and waste the user's time.
    """
    runtime = await probe_runtime(base_url)
    if not runtime.reachable:
        raise HTTPException(
            status_code=503,
            detail="Ollama runtime is not reachable; start Ollama before running the probe.",
        )

    config_path = _config_path()
    existing_override = (read_probe_result(config_path) or {}).get("user_override")

    async def event_stream():
        try:
            async for event in iter_probe_events(
                base_url=base_url,
                ollama_version=runtime.version,
            ):
                if event.get("event") == "complete":
                    payload = event["result"]
                    # Reconstruct ProbeResult to reuse persist_probe_result's
                    # locked_config_update path; preserves any prior override
                    # (rerun shouldn't silently drop a user's choice).
                    result = ProbeResult(
                        schema_version=payload["schema_version"],
                        timestamp_utc=payload["timestamp_utc"],
                        ollama_version=payload["ollama_version"],
                        platform=payload["platform"],
                        ram_gb=payload["ram_gb"],
                        recommended_model=payload["recommended_model"],
                        safe_fallback_used=payload["safe_fallback_used"],
                        candidates_evaluated=tuple(
                            ProbeEvidence(**e) for e in payload["candidates_evaluated"]
                        ),
                        user_override=existing_override,
                        catalog_models=tuple(payload.get("catalog_models") or ()),
                    )
                    persist_probe_result(
                        result,
                        config_path=config_path,
                        user_override=existing_override,
                    )
                    payload["user_override"] = existing_override
                    event = {"event": "complete", "result": payload}
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:  # noqa: BLE001 — surface any failure to the client
            logger.exception("chat-model-probe stream failed")
            yield f"data: {json.dumps({'event': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat-model-probe/override")
async def set_chat_model_probe_override(req: ChatModelProbeOverrideRequest):
    """Set ``user_override`` on the persisted probe record.

    ``model: null`` clears the override (revert to recommendation). Setting
    an override does not re-run the probe — the user is opting out of the
    automated recommendation.
    """
    set_user_override(_config_path(), model=req.model)
    record = read_probe_result(_config_path()) or {}
    return {
        "status": "ok",
        "user_override": record.get("user_override"),
        "recommended_model": record.get("recommended_model"),
    }


# ── First-run pull orchestrator (ADR 005 §B) ───────────────────────────────


class FirstRunStartRequest(BaseModel):
    """Optional body for ``POST /api/local/first-run/start``.

    ``skip=True`` is the user's "I'll pick my own model later" path: writes
    no marker, sets state='skipped', returns immediately. Per ADR 005 §B
    "Skip / opt-out" — next launch re-prompts.
    """
    skip: bool = False
    base_url: str = DEFAULT_OLLAMA_BASE_URL


@router.post("/first-run/start")
async def start_first_run(req: Optional[FirstRunStartRequest] = None):
    """Kick off the first-run pull pipeline (probe → primary → fallback → probe).

    Idempotent: concurrent calls while a job runs return ``already_running``
    without spawning a second task. Calls after completion (marker file
    present) return ``already_complete``. The skip path is the only flow
    that does NOT write the marker.
    """
    body = req or FirstRunStartRequest()
    result = await first_run_orchestrator.start_async(
        skip=body.skip,
        base_url=body.base_url,
    )
    return result


@router.get("/first-run/status")
async def get_first_run_status():
    """Snapshot the orchestrator state machine + per-pull progress.

    Frontend polls this once per second while the modal is open to drive
    the progress UI. Same shape contract as ``/api/memory/reindex/status``
    (G5) — `state` / `started_at` / `finished_at` / `last_error` plus the
    pipeline-specific fields. Marker presence is reflected in
    `marker_written` so the frontend can decide whether the modal needs to
    show at all on next mount.
    """
    status = first_run_orchestrator.current_status()
    return {
        **status.to_dict(),
        "marker_present": first_run_orchestrator.is_first_run_complete(),
    }
