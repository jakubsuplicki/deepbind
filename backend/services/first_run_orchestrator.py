"""First-run pull orchestrator (ADR 005 §B).

Implements the state machine that runs once on the user's first launch with
no prior chat model. The pipeline:

    probing  →  pulling_primary  →  pulling_fallback  →  running_probe  →  complete
                       │                    │                    │
                       ▼                    ▼                    ▼
                marker written        runtime ladder       chat model pinned
                (chat is usable)     fully populated      to (probed) primary

Mirrors the singleton-supervisor shape of `services/reindex_supervisor.py`
(ADR 003 §I): module-level state, asyncio.Lock for re-entrancy, single
asyncio.Task lifetime, lifespan-cancel hook on FastAPI shutdown.

Two divergences from the reindex pattern:

  1. **The marker file is written mid-pipeline.** As soon as the foreground
     primary pull lands, we write `<workspace>/app/.first_run_complete` —
     the user can chat *now*; the fallback pull and probe continue in the
     background. Subsequent launches see the marker and skip the entire
     pipeline. Per ADR §B step 6.

  2. **Fallback pull failure is non-fatal.** If the primary lands but the
     fallback pull errors out, chat still works — the only consequence is
     that the runtime memory-pressure auto-downgrade has nothing on disk
     to fall back to (G4b4 will lazy-pull on first OOM in that case).
     Status records `fallback_failed=True`; state still advances to
     `complete`. Probe failure is also non-fatal — the user gets a
     functional-but-unprobed chat model and the settings page surfaces a
     "re-run probe" affordance per ADR 012.

Skip path (`/start { skip: true }`): writes no marker, sets `state="skipped"`,
returns immediately. Next launch re-prompts. Per ADR §B "Skip / opt-out."
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from services.ollama_service import (
    DEFAULT_OLLAMA_BASE_URL,
    HardwareProfile,
    ModelCatalogEntry,
    downgrade_ladder_for,
    first_run_default_for,
    probe_hardware,
    pull_model_events,
    tier_for_hardware,
)

logger = logging.getLogger(__name__)

JobState = Literal[
    "idle",              # never started, or no marker present and waiting on user kickoff
    "probing",           # tier_for_hardware in progress
    "pulling_primary",   # foreground pull blocking chat-ready UI state
    "pulling_fallback",  # background pull (primary done, marker written, chat usable)
    "running_probe",     # ADR 012 chat-model-probe (both pulls done)
    "complete",          # marker written, both pulls + probe done (or non-fatal fallback/probe failure)
    "skipped",           # user chose "I'll pick my own model later"
    "failed",            # fatal — primary pull errored before marker write
]

MARKER_FILENAME = ".first_run_complete"
MARKER_SCHEMA_VERSION = 1


@dataclass
class PullProgress:
    """A snapshot of one in-flight Ollama pull.

    `completed` / `total` are bytes counts from Ollama's `/api/pull` event
    stream (the `downloading` event carries them). `status` is the most
    recent status string ("pulling manifest", "downloading", "verifying
    sha256 digest", "writing manifest", "success", or our internal "error").
    `error` is set only on failure.
    """
    model: Optional[str] = None
    status: str = "idle"
    completed: int = 0
    total: int = 0
    error: Optional[str] = None

    @property
    def progress_pct(self) -> float:
        if self.total == 0:
            return 0.0
        return min(100.0, round(100.0 * self.completed / self.total, 1))

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "status": self.status,
            "completed": self.completed,
            "total": self.total,
            "progress_pct": self.progress_pct,
            "error": self.error,
        }


@dataclass
class FirstRunStatus:
    state: JobState = "idle"
    tier: Optional[str] = None  # "A" / "B" / "C" — set after probing
    primary_model_id: Optional[str] = None  # catalog id (e.g. "qwen3-8b")
    primary_ollama_model: Optional[str] = None  # ollama tag (e.g. "qwen3:8b")
    fallback_model_id: Optional[str] = None
    fallback_ollama_model: Optional[str] = None
    primary: PullProgress = field(default_factory=PullProgress)
    fallback: PullProgress = field(default_factory=PullProgress)
    probe_failed: bool = False
    fallback_failed: bool = False
    started_at: Optional[float] = None
    primary_completed_at: Optional[float] = None
    finished_at: Optional[float] = None
    last_error: Optional[str] = None
    marker_written: bool = False

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "tier": self.tier,
            "primary_model_id": self.primary_model_id,
            "primary_ollama_model": self.primary_ollama_model,
            "fallback_model_id": self.fallback_model_id,
            "fallback_ollama_model": self.fallback_ollama_model,
            "primary": self.primary.to_dict(),
            "fallback": self.fallback.to_dict(),
            "probe_failed": self.probe_failed,
            "fallback_failed": self.fallback_failed,
            "started_at": self.started_at,
            "primary_completed_at": self.primary_completed_at,
            "finished_at": self.finished_at,
            "last_error": self.last_error,
            "marker_written": self.marker_written,
        }


_status = FirstRunStatus()
_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None


# ── Marker file helpers ─────────────────────────────────────────────────────


def marker_path(workspace_path: Optional[Path] = None) -> Path:
    """Resolve the marker file path. Writes alongside `app/config.json`."""
    if workspace_path is None:
        from config import get_settings
        workspace_path = get_settings().workspace_path
    return workspace_path / "app" / MARKER_FILENAME


def is_first_run_complete(workspace_path: Optional[Path] = None) -> bool:
    """Marker present → first run already executed; pipeline must skip.

    Per ADR §B step 6 the marker is written when the *primary* lands, not
    when the entire pipeline finishes. So a return of True means "the user
    has at least the primary model on disk"; the fallback or probe may
    still be incomplete from a prior aborted run, but that's a runtime
    concern — not a re-prompt trigger.
    """
    return marker_path(workspace_path).exists()


def _write_marker(
    workspace_path: Path,
    *,
    tier: str,
    primary_ollama_model: str,
    skipped: bool = False,
) -> None:
    path = marker_path(workspace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": MARKER_SCHEMA_VERSION,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "tier": tier,
        "primary_model": primary_ollama_model,
        "skipped": skipped,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ── Public read-only views ──────────────────────────────────────────────────


def current_status() -> FirstRunStatus:
    return _status


def is_running() -> bool:
    return _status.state in (
        "probing", "pulling_primary", "pulling_fallback", "running_probe"
    )


def reset_for_tests() -> None:
    """Test helper — wipes module state. Tests don't share lifecycle."""
    global _task, _status
    _task = None
    _status = FirstRunStatus()


# ── Pull loop driver ────────────────────────────────────────────────────────


async def _drive_pull(
    entry: ModelCatalogEntry,
    progress: PullProgress,
    *,
    base_url: str,
) -> bool:
    """Stream one Ollama pull; mutate `progress` in place. Returns True on
    success (final `success` event observed), False on failure."""
    progress.model = entry.ollama_model
    progress.status = "starting"
    progress.completed = 0
    progress.total = 0
    progress.error = None

    saw_success = False
    async for event in pull_model_events(entry.ollama_model, base_url):
        status = str(event.get("status", ""))
        # Ollama signals failure two different ways: (a) `{"status":
        # "error", "error": "..."}` for in-progress failures (rare),
        # and (b) `{"error": "..."}` with no `status` key for upfront
        # rejections — most commonly "pull model manifest: file does
        # not exist" when the catalog references a tag that doesn't
        # exist in the Ollama registry. The single shared branch here
        # covers both shapes; without the `event.get("error")` check
        # the upfront-rejection path used to fall through to "stream
        # ended without success event", which obscured the real cause.
        if status == "error" or event.get("error"):
            progress.status = "error"
            progress.error = str(event.get("error", "unknown error"))
            return False
        progress.status = status
        # Ollama's `downloading` events carry byte counters; non-download
        # status strings do not. Keep the last download's totals visible
        # through the manifest-write/verify tail so the UI doesn't snap
        # progress back to 0 in the final 5%.
        if "completed" in event:
            try:
                progress.completed = int(event["completed"])
            except (TypeError, ValueError):
                pass
        if "total" in event:
            try:
                progress.total = int(event["total"])
            except (TypeError, ValueError):
                pass
        if status == "success":
            saw_success = True

    if not saw_success and progress.error is None:
        # Stream ended without a success or error event — treat as failure.
        progress.status = "error"
        progress.error = "pull stream ended without success event"
        return False

    return saw_success


# ── Pipeline ────────────────────────────────────────────────────────────────


async def _run_probe(base_url: str) -> bool:
    """Run the ADR 012 chat-model-probe; persist on complete.

    Returns True when the probe completed and persisted, False otherwise.
    Probe failure is non-fatal — the orchestrator records it and continues
    to `complete` so the user can still chat against the primary.
    """
    from config import get_settings
    from services.chat_model_probe import (
        ProbeEvidence,
        ProbeResult,
        iter_probe_events,
        persist_probe_result,
        read_probe_result,
    )
    from services.ollama_service import probe_runtime

    runtime = await probe_runtime(base_url)
    if not runtime.reachable:
        logger.info("first_run: probe skipped — Ollama runtime unreachable")
        return False

    config_path = get_settings().workspace_path / "app" / "config.json"
    existing_override = (read_probe_result(config_path) or {}).get("user_override")

    completed = False
    async for event in iter_probe_events(
        base_url=base_url,
        ollama_version=runtime.version,
    ):
        if event.get("event") == "complete":
            payload = event["result"]
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
                # Persist the catalog snapshot the probe ran against so
                # `needs_rerun()` has something to compare current state to.
                # Without this, the field defaults to an empty tuple and
                # every subsequent boot computes
                # `set(current_models) - set([]) = all current models`,
                # firing the "A new local model is available — re-run to
                # consider it" banner forever.
                catalog_models=tuple(payload.get("catalog_models") or ()),
            )
            persist_probe_result(
                result,
                config_path=config_path,
                user_override=existing_override,
            )
            completed = True

    return completed


async def _pipeline(
    *,
    workspace_path: Path,
    base_url: str,
) -> None:
    """Drive the full first-run pipeline. Updates module-level _status."""

    # ── Stage 1: probe hardware → tier ──────────────────────────────────
    _status.state = "probing"
    try:
        hw: HardwareProfile = probe_hardware()
        tier = tier_for_hardware(hw)
    except Exception as exc:  # noqa: BLE001
        logger.exception("first_run: probe_hardware failed")
        _status.state = "failed"
        _status.last_error = f"{type(exc).__name__}: {exc}"
        _status.finished_at = time.time()
        return

    _status.tier = tier
    primary = first_run_default_for(tier)
    if primary is None:
        _status.state = "failed"
        _status.last_error = f"no first-run default registered for tier {tier}"
        _status.finished_at = time.time()
        return

    _status.primary_model_id = primary.id
    _status.primary_ollama_model = primary.ollama_model

    ladder = downgrade_ladder_for(tier, include_opt_in=False)
    fallback: Optional[ModelCatalogEntry] = None
    for entry in ladder:
        if entry.id != primary.id and entry.ladder_positions[tier] < primary.ladder_positions[tier]:
            fallback = entry
            break
    if fallback is not None:
        _status.fallback_model_id = fallback.id
        _status.fallback_ollama_model = fallback.ollama_model

    # ── Stage 2: foreground primary pull ────────────────────────────────
    _status.state = "pulling_primary"
    primary_ok = await _drive_pull(primary, _status.primary, base_url=base_url)
    if not primary_ok:
        _status.state = "failed"
        _status.last_error = _status.primary.error or "primary pull failed"
        _status.finished_at = time.time()
        return

    # Primary on disk → write the marker (ADR §B step 6) so subsequent
    # launches skip the pipeline even if we crash before fallback / probe.
    try:
        _write_marker(
            workspace_path,
            tier=tier,
            primary_ollama_model=primary.ollama_model,
        )
        _status.marker_written = True
    except Exception as exc:  # noqa: BLE001 — non-fatal; we still own the model
        logger.warning("first_run: marker write failed: %s", exc)

    _status.primary_completed_at = time.time()

    # ── Stage 3: background fallback pull ───────────────────────────────
    if fallback is not None:
        _status.state = "pulling_fallback"
        fallback_ok = await _drive_pull(fallback, _status.fallback, base_url=base_url)
        if not fallback_ok:
            # Non-fatal — primary still works. Surface in status so the
            # frontend can warn that the runtime ladder isn't fully primed.
            _status.fallback_failed = True
            logger.info(
                "first_run: fallback pull failed (%s); chat still works against primary",
                _status.fallback.error,
            )

    # ── Stage 4: chat-model-probe ───────────────────────────────────────
    _status.state = "running_probe"
    try:
        probed = await _run_probe(base_url)
        if not probed:
            _status.probe_failed = True
    except Exception as exc:  # noqa: BLE001 — non-fatal
        logger.exception("first_run: probe raised; continuing")
        _status.probe_failed = True
        _status.last_error = f"probe: {type(exc).__name__}: {exc}"

    # ── Stage 5: complete ───────────────────────────────────────────────
    _status.state = "complete"
    _status.finished_at = time.time()


# ── Public lifecycle entry points ───────────────────────────────────────────


async def start_async(
    *,
    skip: bool = False,
    workspace_path: Optional[Path] = None,
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
) -> Dict[str, Any]:
    """Idempotent kick-off.

    `skip=True` is the user's "I'll pick my own model later" path: writes no
    marker, sets state='skipped', returns immediately. Next launch re-prompts.

    Returns a small dict the endpoint reflects to the client:
      {"result": "started"|"already_running"|"already_complete"|"skipped",
       "tier": "A"|None, "primary_model_id": "qwen3-8b"|None}

    Concurrent /start calls while a job is in flight are no-ops; the
    second caller gets ``"already_running"`` without spawning a second task.
    """
    global _task

    from config import get_settings
    ws = workspace_path or get_settings().workspace_path

    async with _lock:
        if skip:
            _status.state = "skipped"
            _status.started_at = time.time()
            _status.finished_at = time.time()
            return {"result": "skipped"}

        if is_running():
            return {
                "result": "already_running",
                "tier": _status.tier,
                "primary_model_id": _status.primary_model_id,
            }
        if _status.state == "complete":
            return {
                "result": "already_complete",
                "tier": _status.tier,
                "primary_model_id": _status.primary_model_id,
            }
        if is_first_run_complete(ws) and _status.state == "idle":
            # Marker exists from a prior session; reflect that without
            # re-running the pipeline.
            _status.state = "complete"
            _status.marker_written = True
            return {
                "result": "already_complete",
                "tier": _status.tier,
                "primary_model_id": _status.primary_model_id,
            }

        # Reset transient fields and seed a fresh run.
        _status.state = "probing"
        _status.tier = None
        _status.primary_model_id = None
        _status.primary_ollama_model = None
        _status.fallback_model_id = None
        _status.fallback_ollama_model = None
        _status.primary = PullProgress()
        _status.fallback = PullProgress()
        _status.probe_failed = False
        _status.fallback_failed = False
        _status.started_at = time.time()
        _status.primary_completed_at = None
        _status.finished_at = None
        _status.last_error = None
        _status.marker_written = False

    async def _runner() -> None:
        try:
            await _pipeline(workspace_path=ws, base_url=base_url)
        except asyncio.CancelledError:
            # Clean shutdown — preserve current state for the next read.
            if _status.state in (
                "probing", "pulling_primary", "pulling_fallback", "running_probe"
            ):
                _status.last_error = "cancelled"
                _status.finished_at = time.time()
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("first_run: pipeline crashed")
            _status.state = "failed"
            _status.last_error = f"{type(exc).__name__}: {exc}"
            _status.finished_at = time.time()

    _task = asyncio.create_task(_runner(), name="jarvis-first-run-orchestrator")
    return {"result": "started"}


async def cancel_and_wait() -> None:
    """Cancel the in-flight task (if any) and wait for it to settle.

    Called from FastAPI's lifespan exit. Safe to call when no task is
    running. Forces the state to ``failed`` (not idle) when a cancellation
    arrives mid-pipeline because the marker may or may not have been
    written — the next launch's idempotent `start_async()` path uses
    `is_first_run_complete()` to decide whether to re-run, not the
    transient module state.
    """
    global _task
    if _task is None:
        return
    if not _task.done():
        _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None
    if _status.state in (
        "probing", "pulling_primary", "pulling_fallback", "running_probe"
    ):
        _status.state = "failed"
        _status.finished_at = time.time()
        _status.last_error = "cancelled"


async def wait_for_test() -> FirstRunStatus:
    """Test helper — block until the current task settles, then return status."""
    global _task
    if _task is not None:
        try:
            await _task
        except Exception:  # noqa: BLE001
            pass
    return _status
