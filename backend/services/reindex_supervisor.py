"""Background-task supervisor for the embedding reindex pass (ADR 003 §I).

The Markdown→SQLite reindex (`memory_service.reindex_all`) stays synchronous in
the FastAPI lifespan because it finishes in well under a second on multi-
thousand-note vaults — it's a single SQL truncate plus a directory walk.

The expensive pass — fastembed CPU inference over every note that has changed
since last embedding — is what we move into a background task so the UI is
interactive immediately after startup, even on a cold vault. Per ADR 003 §I:

    > First launch on a 5k-note vault would otherwise block the UI for tens
    > of seconds while embeddings warm up. Move it behind a status endpoint
    > and surface a non-blocking toast in the frontend.

This module owns three things:

    1. A module-level `ReindexStatus` snapshot (single-process, single-user
       — desktop scope; no cross-instance coordination required).
    2. `start_async()` — idempotent kick-off. If a job is already running,
       it returns "already_running" and does NOT spawn a second task.
    3. `current_status()` — read-only view for the `/api/memory/reindex/status`
       endpoint and any in-process callers.

The supervisor does NOT replace `embedding_service.reindex_all()` — the latter
is still callable directly from tests and one-off scripts. The supervisor is a
*lifecycle* layer on top of it: state, single-flight, cancellable on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)

JobState = Literal["idle", "running", "failed"]


@dataclass
class ReindexStatus:
    state: JobState = "idle"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    scanned: int = 0
    total: int = 0
    last_error: Optional[str] = None
    last_run_count: int = 0

    @property
    def progress_pct(self) -> float:
        if self.total == 0:
            return 0.0 if self.state == "idle" else 100.0
        return min(100.0, round(100.0 * self.scanned / self.total, 1))

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "scanned": self.scanned,
            "total": self.total,
            "progress_pct": self.progress_pct,
            "last_error": self.last_error,
            "last_run_count": self.last_run_count,
        }


_status = ReindexStatus()
_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None


def current_status() -> ReindexStatus:
    return _status


def is_running() -> bool:
    return _status.state == "running"


def reset_for_tests() -> None:
    """Test helper — wipes module state so tests don't leak across each other."""
    global _task
    _task = None
    _status.state = "idle"
    _status.started_at = None
    _status.finished_at = None
    _status.scanned = 0
    _status.total = 0
    _status.last_error = None
    _status.last_run_count = 0


async def _embed_pass(workspace_path: Optional[Path] = None) -> int:
    """The actual reindex body. Walks the vault, calls embed_note +
    embed_note_chunks for each markdown file. Updates _status.scanned/total
    incrementally so the status endpoint reports useful progress.

    Returns the number of notes that produced a new embedding (skipping
    unchanged-content notes per embed_note's hash check).
    """
    from config import get_settings
    from services.embedding_service import embed_note, embed_note_chunks, is_available
    from utils.markdown import parse_frontmatter

    if not is_available():
        logger.info("reindex_supervisor: fastembed not installed, skipping")
        return 0

    ws = workspace_path or get_settings().workspace_path
    mem = ws / "memory"
    db_path = ws / "app" / "jarvis.db"
    if not mem.exists():
        return 0

    md_files = list(mem.rglob("*.md"))
    _status.total = len(md_files)
    _status.scanned = 0

    embedded = 0
    for md_file in md_files:
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
            rel_path = str(md_file.relative_to(mem))
            if await embed_note(rel_path, content, db_path):
                embedded += 1
            try:
                fm, _body = parse_frontmatter(content)
                subject_type = str(fm.get("type") or "note")
                await embed_note_chunks(
                    rel_path, content, db_path, subject_type=subject_type
                )
            except Exception as exc:
                logger.debug("chunk embed failed for %s: %s", rel_path, exc)
        except Exception as exc:
            # Don't let one bad file kill the whole pass — log and move on.
            logger.warning("reindex skipped %s: %s", md_file, exc)
        finally:
            _status.scanned += 1

    return embedded


async def start_async(
    workspace_path: Optional[Path] = None,
) -> Literal["started", "already_running"]:
    """Idempotent kick-off. Spawns the embedding pass as an asyncio task and
    returns immediately. Subsequent calls while running return without
    spawning a second task.
    """
    global _task

    async with _lock:
        if _status.state == "running":
            return "already_running"
        _status.state = "running"
        _status.started_at = time.time()
        _status.finished_at = None
        _status.last_error = None
        _status.scanned = 0
        _status.total = 0

    async def _runner() -> None:
        try:
            count = await _embed_pass(workspace_path)
            _status.last_run_count = count
            _status.finished_at = time.time()
            _status.state = "idle"
            logger.info(
                "reindex finished: embedded=%d, scanned=%d/%d in %.2fs",
                count,
                _status.scanned,
                _status.total,
                (_status.finished_at or 0) - (_status.started_at or 0),
            )
        except asyncio.CancelledError:
            _status.state = "idle"
            _status.finished_at = time.time()
            raise
        except Exception as exc:  # noqa: BLE001 — we want to record any failure
            logger.exception("reindex_supervisor: pass failed")
            _status.state = "failed"
            _status.last_error = f"{type(exc).__name__}: {exc}"
            _status.finished_at = time.time()

    _task = asyncio.create_task(_runner(), name="jarvis-reindex-supervisor")
    return "started"


async def cancel_and_wait() -> None:
    """Cancel the in-flight task (if any) and wait for it to settle. Called
    from FastAPI's lifespan exit so a server shutdown doesn't leave a dangling
    embedding task. Safe to call when no task is running.

    Forces ``state="idle"`` after the await so a cancellation that arrives
    *before* the runner's first await (i.e. before its except-CancelledError
    block runs) still leaves consistent state.
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
    if _status.state == "running":
        _status.state = "idle"
        _status.finished_at = time.time()


async def wait_for_test() -> ReindexStatus:
    """Test helper — block until the current task settles, then return status."""
    global _task
    if _task is not None:
        try:
            await _task
        except Exception:  # noqa: BLE001
            pass
    return _status
