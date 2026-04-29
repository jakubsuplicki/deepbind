"""In-memory ingest job tracker.

Tracks active file/URL ingest operations so the UI can display a global
status badge ("Ingesting 3/6 files…") next to the ALIVE indicator while
the user navigates the app.

This is intentionally process-local and ephemeral. Background workers and
ingest endpoints call ``start_job`` when work begins and ``finish_job``
when it ends (success or failure). The frontend polls
``GET /api/memory/ingest/status`` every few seconds.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class IngestJob:
    id: str
    name: str
    # "file" | "url" | "youtube" | "graph_rebuild" | "embed" | "section_connect"
    kind: str
    started_at: float
    size_bytes: Optional[int] = None
    status: str = "running"  # "running" | "done" | "failed"
    stage: str = "processing"  # "uploading" | "extracting" | "indexing" | "embedding" | "linking" | "done"
    error: Optional[str] = None
    finished_at: Optional[float] = None


_lock = threading.Lock()
_jobs: Dict[str, IngestJob] = {}
# Recently finished jobs are kept briefly so the UI can show "✓ done" before fading.
_FINISHED_TTL_S = 8.0

# Separate flag for the background graph rebuild so we only run one at a time
# and can report its progress as a dedicated job in the status snapshot.
_rebuild_job_id: Optional[str] = None


def _resolve_memory_dir(workspace_path: Optional[Path]) -> Path:
    """Resolve the workspace's memory dir without importing inside hot paths."""
    from config import get_settings
    base = Path(workspace_path) if workspace_path else get_settings().workspace_path
    return base / "memory"


def _resolve_db_path(workspace_path: Optional[Path]) -> Path:
    from config import get_settings
    base = Path(workspace_path) if workspace_path else get_settings().workspace_path
    return base / "app" / "jarvis.db"


async def embed_paths(
    paths: List[str],
    *,
    workspace_path: Optional[Path] = None,
    job_id: Optional[str] = None,
) -> None:
    """Embed note + chunk vectors for a list of paths sequentially.

    Extracted so unit tests can invoke the embedding pass directly
    without going through the daemon-thread scheduler. ``job_id`` is
    optional; when set, ``update_stage`` reports per-note progress.
    """
    import logging
    log = logging.getLogger(__name__)
    from services.embedding_service import embed_note, embed_note_chunks
    from utils.markdown import parse_frontmatter

    mem = _resolve_memory_dir(workspace_path)
    db_path = _resolve_db_path(workspace_path)
    total = len(paths)

    for idx, rel_path in enumerate(paths):
        if job_id:
            update_stage(job_id, f"embedding {idx + 1}/{total}")
        file_path = mem / rel_path
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("embed job: cannot read %s: %s", rel_path, exc)
            continue
        try:
            await embed_note(rel_path, content, db_path)
        except Exception as exc:
            log.warning("embed_note failed for %s: %s", rel_path, exc)
        try:
            fm, _body = parse_frontmatter(content)
            subject_type = str(fm.get("type") or "note")
            await embed_note_chunks(
                rel_path, content, db_path, subject_type=subject_type
            )
        except Exception as exc:
            log.warning(
                "embed_note_chunks failed for %s: %s", rel_path, exc
            )


def schedule_embed_for_paths(
    paths: List[str],
    *,
    workspace_path: Optional[Path] = None,
    doc_title: str = "",
) -> Optional[str]:
    """Background job that embeds note + chunk vectors for a list of paths.

    ADR 013 knob-6: ``_emit_document_sections`` writes each section's MD
    file and indexes it WITHOUT embedding (``defer_embedding=True``). The
    HTTP response returns once that synchronous portion finishes. This
    function fires a daemon thread that walks the listed paths and runs
    ``embed_note`` + ``embed_note_chunks`` for each, marking progress
    through ``update_stage`` so the UI badge shows "embedding 12/60…".

    Returns the job id, or ``None`` if no paths were supplied.
    """
    if not paths:
        return None

    # Same escape hatch as the inline embed path in memory_service._index_note —
    # tests that don't care about embeddings can short-circuit the daemon
    # thread and avoid leaking work past test teardown.
    import os
    if os.environ.get("JARVIS_DISABLE_EMBEDDINGS") == "1":
        return None

    name = f"Embedding: {doc_title}" if doc_title else "Embedding sections"
    job_id = start_job(name, kind="embed")

    def _run() -> None:
        import asyncio
        import logging
        log = logging.getLogger(__name__)
        try:
            asyncio.run(
                embed_paths(paths, workspace_path=workspace_path, job_id=job_id)
            )
            finish_job(job_id)
        except Exception as exc:
            log.warning("Background embed job %s failed: %s", job_id, exc)
            finish_job(job_id, error=str(exc))

    t = threading.Thread(target=_run, daemon=True, name=f"embed-{job_id}")
    t.start()
    return job_id


def schedule_graph_rebuild(workspace_path=None) -> None:
    """Fire a background rebuild_graph() if none is already running.

    Called automatically after each file ingest.  The rebuild is tracked as a
    special ``kind="graph_rebuild"`` job so the frontend can show a
    "Building graph…" indicator without blocking the ingest response.
    """
    global _rebuild_job_id
    with _lock:
        # Skip if a rebuild job is already running.
        if _rebuild_job_id is not None:
            existing = _jobs.get(_rebuild_job_id)
            if existing is not None and existing.status == "running":
                return

    job_id = start_job("Graph rebuild", kind="graph_rebuild")
    with _lock:
        _rebuild_job_id = job_id

    def _run():
        import logging
        log = logging.getLogger(__name__)
        try:
            update_stage(job_id, "rebuilding")
            from services.graph_service import rebuild_graph
            rebuild_graph(workspace_path=workspace_path)
            finish_job(job_id)
        except Exception as exc:
            log.warning("Background graph rebuild failed: %s", exc)
            finish_job(job_id, error=str(exc))

    t = threading.Thread(target=_run, daemon=True, name="graph-rebuild")
    t.start()


def _prune_finished_locked() -> None:
    now = time.time()
    expired = [
        jid for jid, job in _jobs.items()
        if job.finished_at is not None and (now - job.finished_at) > _FINISHED_TTL_S
    ]
    for jid in expired:
        _jobs.pop(jid, None)


def start_job(name: str, *, kind: str = "file", size_bytes: Optional[int] = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _prune_finished_locked()
        _jobs[job_id] = IngestJob(
            id=job_id,
            name=name,
            kind=kind,
            started_at=time.time(),
            size_bytes=size_bytes,
        )
    return job_id


def finish_job(job_id: str, *, error: Optional[str] = None) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.status = "failed" if error else "done"
        job.stage = "failed" if error else "done"
        job.error = error
        job.finished_at = time.time()
        _prune_finished_locked()


def update_stage(job_id: str, stage: str) -> None:
    """Mark which step the job is currently on (extracting / indexing / ...)."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.stage = stage


def snapshot() -> dict:
    """Return a JSON-serialisable snapshot of current ingest state."""
    with _lock:
        _prune_finished_locked()
        running: List[dict] = []
        recently_done: List[dict] = []
        for job in _jobs.values():
            payload = {
                "id": job.id,
                "name": job.name,
                "kind": job.kind,
                "size_bytes": job.size_bytes,
                "status": job.status,
                "stage": job.stage,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "error": job.error,
            }
            if job.status == "running":
                running.append(payload)
            else:
                recently_done.append(payload)
        return {
            "active_count": len(running),
            "active": running,
            "recent": recently_done,
        }
