from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

import aiosqlite

from config import get_settings
from services.memory_service import NoteNotFoundError, delete_note
from services.source_import.manifest import (
    get_batch_runtime,
    get_batch_summary,
    update_batch_state,
)
from services.source_import.models import (
    SourceImportBatchSummary,
    SourceImportFileOutcome,
)


logger = logging.getLogger(__name__)


_REMOVABLE_STATES = {"completed", "failed", "cancelled", "interrupted"}
_RUNNING_STATES = {"queued", "importing", "cancelling", "removing"}


class SourceImportRemovalConflict(Exception):
    """Raised when a batch cannot be removed in its current lifecycle state."""


def _workspace_path(workspace_path: Optional[Path] = None) -> Path:
    return workspace_path or get_settings().workspace_path


def _db_path(workspace_path: Optional[Path] = None) -> Path:
    return _workspace_path(workspace_path) / "app" / "jarvis.db"


def _batched(values: list[str], size: int = 200) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _manifest_note_paths(files: list[SourceImportFileOutcome]) -> list[str]:
    paths: list[str] = []
    for item in files:
        for note_path in item.note_paths:
            clean = str(note_path).strip()
            if clean:
                paths.append(clean)
    return list(dict.fromkeys(paths))


async def _cleanup_derived_rows(
    note_paths: list[str],
    *,
    workspace_path: Optional[Path] = None,
) -> None:
    if not note_paths:
        return

    db_path = _db_path(workspace_path)
    if not db_path.exists():
        return

    async with aiosqlite.connect(str(db_path)) as db:
        from services._db import apply_pragmas

        await apply_pragmas(db)
        for chunk in _batched(note_paths):
            placeholders = ",".join("?" for _ in chunk)
            await db.execute(
                f"DELETE FROM note_embeddings WHERE path IN ({placeholders})",
                chunk,
            )
            await db.execute(
                f"DELETE FROM chunk_embeddings WHERE path IN ({placeholders})",
                chunk,
            )
            await db.execute(
                f"DELETE FROM note_chunks WHERE path IN ({placeholders})",
                chunk,
            )
            await db.execute(
                f"DELETE FROM alias_index WHERE note_path IN ({placeholders})",
                chunk,
            )
            await db.execute(
                f"""
                DELETE FROM enrichments
                WHERE subject_type = 'note'
                  AND subject_id IN ({placeholders})
                """,
                chunk,
            )
            await db.execute(
                f"""
                DELETE FROM enrichment_queue
                WHERE subject_type = 'note'
                  AND subject_id IN ({placeholders})
                """,
                chunk,
            )
            await db.execute(
                f"""
                DELETE FROM dismissed_suggestions
                WHERE note_path IN ({placeholders})
                   OR target_path IN ({placeholders})
                """,
                [*chunk, *chunk],
            )

            node_ids = [f"note:{path}" for path in chunk]
            node_placeholders = ",".join("?" for _ in node_ids)
            await db.execute(
                f"DELETE FROM node_embeddings WHERE node_id IN ({node_placeholders})",
                node_ids,
            )

        await db.commit()


async def remove_import_batch(
    *,
    batch_id: str,
    confirm_batch_id: str,
    workspace_path: Optional[Path] = None,
) -> SourceImportBatchSummary:
    if confirm_batch_id != batch_id:
        raise ValueError("Batch id confirmation does not match")

    batch, files = await get_batch_runtime(batch_id, workspace_path=workspace_path)
    state = str(batch["state"])

    if state == "removed":
        return await get_batch_summary(batch_id, workspace_path=workspace_path)
    if state in _RUNNING_STATES:
        raise SourceImportRemovalConflict("Import is still running")
    if state not in _REMOVABLE_STATES:
        raise SourceImportRemovalConflict("Import is not removable in its current state")

    note_paths = _manifest_note_paths(files)
    await update_batch_state(
        batch_id,
        "removing",
        current_file=None,
        workspace_path=workspace_path,
    )

    cleanup_note_paths: list[str] = []
    failures: list[str] = []
    for note_path in note_paths:
        try:
            await delete_note(note_path, workspace_path=workspace_path)
            cleanup_note_paths.append(note_path)
        except NoteNotFoundError:
            # Missing notes are treated as already gone; the manifest should still
            # be able to complete removal and clear derived rows below.
            cleanup_note_paths.append(note_path)
            continue
        except Exception as exc:
            logger.warning(
                "Source import note removal failed for %s: %s",
                note_path,
                exc,
            )
            failures.append(note_path)

    await _cleanup_derived_rows(cleanup_note_paths, workspace_path=workspace_path)

    if failures:
        await update_batch_state(
            batch_id,
            state,
            current_file=None,
            workspace_path=workspace_path,
        )
        raise SourceImportRemovalConflict(
            f"Could not remove {len(failures)} imported note(s)"
        )

    await update_batch_state(
        batch_id,
        "removed",
        current_file=None,
        finished=True,
        workspace_path=workspace_path,
    )

    try:
        from services import ingest_jobs

        ingest_jobs.schedule_graph_rebuild(workspace_path=workspace_path)
    except Exception as exc:
        logger.warning("Source import removal graph rebuild scheduling failed: %s", exc)

    return await get_batch_summary(batch_id, workspace_path=workspace_path)
