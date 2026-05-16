from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import aiosqlite

from config import get_settings
from services.source_import.models import (
    SourceImportBatchSummary,
    SourceImportFileOutcome,
    SourceImportFileStatus,
    SourceImportState,
    SourceScanFileItem,
    SourceScanResult,
    SourceSelectionRecord,
)


_INITIALIZED_DB_PATHS: set[str] = set()


SOURCE_IMPORT_SQL = """
CREATE TABLE IF NOT EXISTS source_import_batches (
    batch_id            TEXT PRIMARY KEY,
    scan_id             TEXT NOT NULL,
    selection_id        TEXT NOT NULL,
    source_kind         TEXT NOT NULL DEFAULT 'local_folder',
    source_display_name TEXT NOT NULL,
    source_root_path    TEXT NOT NULL,
    destination_root    TEXT NOT NULL,
    state               TEXT NOT NULL,
    total_file_count    INTEGER NOT NULL DEFAULT 0,
    total_bytes         INTEGER NOT NULL DEFAULT 0,
    created_note_count  INTEGER NOT NULL DEFAULT 0,
    current_file        TEXT,
    started_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    finished_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_import_batches_started
    ON source_import_batches(started_at);

CREATE TABLE IF NOT EXISTS source_import_files (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       TEXT NOT NULL,
    file_id        TEXT NOT NULL,
    relpath        TEXT NOT NULL,
    filename       TEXT NOT NULL,
    extension      TEXT NOT NULL,
    size           INTEGER NOT NULL DEFAULT 0,
    modified_at    TEXT,
    status         TEXT NOT NULL,
    stage          TEXT,
    reason         TEXT,
    duplicate_of   TEXT,
    content_hash   TEXT,
    note_paths     TEXT NOT NULL DEFAULT '[]',
    updated_at     TEXT NOT NULL,
    UNIQUE(batch_id, file_id),
    FOREIGN KEY (batch_id) REFERENCES source_import_batches(batch_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_source_import_files_batch
    ON source_import_files(batch_id);
CREATE INDEX IF NOT EXISTS idx_source_import_files_status
    ON source_import_files(status);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_path(workspace_path: Optional[Path] = None) -> Path:
    return workspace_path or get_settings().workspace_path


def _db_path(workspace_path: Optional[Path] = None) -> Path:
    return _workspace_path(workspace_path) / "app" / "jarvis.db"


async def _ensure_db(workspace_path: Optional[Path] = None) -> Path:
    from models.database import init_database

    db_path = _db_path(workspace_path)
    key = str(db_path)
    if key not in _INITIALIZED_DB_PATHS:
        await init_database(db_path)
        _INITIALIZED_DB_PATHS.add(key)
    return db_path


def _public_destination(destination_root: str) -> str:
    cleaned = destination_root.strip("/")
    return f"memory/{cleaned}/" if cleaned else "memory/"


def _decode_note_paths(value: object) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _outcome_from_row(row: aiosqlite.Row) -> SourceImportFileOutcome:
    return SourceImportFileOutcome(
        file_id=row["file_id"],
        relpath=row["relpath"],
        filename=row["filename"],
        extension=row["extension"],
        size=int(row["size"] or 0),
        modified_at=row["modified_at"],
        status=row["status"],
        stage=row["stage"],
        reason=row["reason"],
        duplicate_of=row["duplicate_of"],
        content_hash=row["content_hash"],
        note_paths=_decode_note_paths(row["note_paths"]),
    )


_ACTIVE_STATES = ("queued", "importing", "cancelling", "removing")


async def create_batch_manifest(
    *,
    batch_id: str,
    scan: SourceScanResult,
    selection: SourceSelectionRecord,
    files: Iterable[SourceScanFileItem],
    destination_root: str,
    workspace_path: Optional[Path] = None,
) -> SourceImportBatchSummary:
    db_path = await _ensure_db(workspace_path)
    now = _now_iso()
    file_list = list(files)
    total_bytes = sum(max(item.size, 0) for item in file_list)
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            INSERT INTO source_import_batches(
                batch_id, scan_id, selection_id, source_kind, source_display_name,
                source_root_path, destination_root, state, total_file_count,
                total_bytes, created_note_count, started_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                scan.report.scan_id,
                selection.summary.selection_id,
                scan.report.source_kind,
                scan.report.source_display_name,
                scan.report.source_root_path,
                destination_root.strip("/"),
                "queued",
                len(file_list),
                total_bytes,
                0,
                now,
                now,
            ),
        )
        for item in file_list:
            await db.execute(
                """
                INSERT INTO source_import_files(
                    batch_id, file_id, relpath, filename, extension, size,
                    modified_at, status, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    item.id,
                    item.relpath,
                    item.filename,
                    item.extension,
                    item.size,
                    item.modified_at,
                    "queued",
                    now,
                ),
            )
        await db.commit()
    return await get_batch_summary(batch_id, workspace_path=workspace_path)


async def list_batch_summaries(
    *,
    limit: int = 20,
    workspace_path: Optional[Path] = None,
) -> list[SourceImportBatchSummary]:
    db_path = await _ensure_db(workspace_path)
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT batch_id FROM source_import_batches
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
    summaries = []
    for row in rows:
        summaries.append(
            await get_batch_summary(row["batch_id"], workspace_path=workspace_path)
        )
    return summaries


async def get_batch_summary(
    batch_id: str,
    *,
    workspace_path: Optional[Path] = None,
) -> SourceImportBatchSummary:
    db_path = await _ensure_db(workspace_path)
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        batch_cursor = await db.execute(
            "SELECT * FROM source_import_batches WHERE batch_id = ?",
            (batch_id,),
        )
        batch = await batch_cursor.fetchone()
        if batch is None:
            raise KeyError("Import batch not found")

        file_cursor = await db.execute(
            """
            SELECT * FROM source_import_files
            WHERE batch_id = ?
            ORDER BY id
            """,
            (batch_id,),
        )
        file_rows = await file_cursor.fetchall()

    files = [_outcome_from_row(row) for row in file_rows]
    imported_file_count = sum(1 for item in files if item.status == "done")
    skipped_file_count = sum(1 for item in files if item.status == "skipped")
    failed_file_count = sum(1 for item in files if item.status == "failed")
    processed_bytes = sum(
        max(item.size, 0)
        for item in files
        if item.status in {"done", "skipped", "failed"}
    )
    return SourceImportBatchSummary(
        batch_id=batch["batch_id"],
        scan_id=batch["scan_id"],
        selection_id=batch["selection_id"],
        source_kind=batch["source_kind"],
        source_display_name=batch["source_display_name"],
        destination_root=_public_destination(batch["destination_root"]),
        state=batch["state"],
        total_file_count=int(batch["total_file_count"] or 0),
        imported_file_count=imported_file_count,
        skipped_file_count=skipped_file_count,
        failed_file_count=failed_file_count,
        created_note_count=int(batch["created_note_count"] or 0),
        total_bytes=int(batch["total_bytes"] or 0),
        processed_bytes=processed_bytes,
        current_file=batch["current_file"],
        files=files,
        started_at=batch["started_at"],
        updated_at=batch["updated_at"],
        finished_at=batch["finished_at"],
    )


async def get_batch_runtime(
    batch_id: str,
    *,
    workspace_path: Optional[Path] = None,
) -> tuple[dict, list[SourceImportFileOutcome]]:
    db_path = await _ensure_db(workspace_path)
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        batch_cursor = await db.execute(
            "SELECT * FROM source_import_batches WHERE batch_id = ?",
            (batch_id,),
        )
        batch = await batch_cursor.fetchone()
        if batch is None:
            raise KeyError("Import batch not found")
        file_cursor = await db.execute(
            "SELECT * FROM source_import_files WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        )
        files = await file_cursor.fetchall()
    return dict(batch), [_outcome_from_row(row) for row in files]


async def update_batch_state(
    batch_id: str,
    state: SourceImportState,
    *,
    current_file: Optional[str] = None,
    created_note_delta: int = 0,
    finished: bool = False,
    workspace_path: Optional[Path] = None,
) -> None:
    db_path = await _ensure_db(workspace_path)
    now = _now_iso()
    finished_at = now if finished else None
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            UPDATE source_import_batches
            SET state = CASE
                    WHEN state = 'cancelling' AND ? = 'importing' THEN state
                    ELSE ?
                END,
                current_file = ?,
                created_note_count = created_note_count + ?,
                updated_at = ?,
                finished_at = CASE WHEN ? THEN ? ELSE finished_at END
            WHERE batch_id = ?
            """,
            (
                state,
                state,
                current_file,
                created_note_delta,
                now,
                1 if finished else 0,
                finished_at,
                batch_id,
            ),
        )
        await db.commit()


async def is_batch_cancellation_requested(
    batch_id: str,
    *,
    workspace_path: Optional[Path] = None,
) -> bool:
    db_path = await _ensure_db(workspace_path)
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            "SELECT state FROM source_import_batches WHERE batch_id = ?",
            (batch_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        raise KeyError("Import batch not found")
    return str(row[0]) in {"cancelling", "cancelled"}


async def request_batch_cancel(
    batch_id: str,
    *,
    workspace_path: Optional[Path] = None,
) -> str:
    db_path = await _ensure_db(workspace_path)
    now = _now_iso()
    async with aiosqlite.connect(str(db_path)) as db:
        cursor = await db.execute(
            "SELECT state FROM source_import_batches WHERE batch_id = ?",
            (batch_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise KeyError("Import batch not found")
        state = str(row[0])
        if state in {"queued", "importing"}:
            await db.execute(
                """
                UPDATE source_import_batches
                SET state = 'cancelling',
                    updated_at = ?
                WHERE batch_id = ?
                """,
                (now, batch_id),
            )
            await db.commit()
            return "cancelling"
        return state


async def mark_unprocessed_files_cancelled(
    batch_id: str,
    *,
    reason: str = "cancelled_by_user",
    workspace_path: Optional[Path] = None,
) -> None:
    db_path = await _ensure_db(workspace_path)
    now = _now_iso()
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            UPDATE source_import_files
            SET status = 'skipped',
                stage = 'cancelled',
                reason = ?,
                updated_at = ?
            WHERE batch_id = ?
              AND status IN ('queued', 'importing')
            """,
            (reason, now, batch_id),
        )
        await db.commit()


async def mark_interrupted_batches(
    *,
    workspace_path: Optional[Path] = None,
) -> int:
    """Mark active manifests from a previous process as interrupted.

    Folder imports are background tasks. If the sidecar exits while a task is
    queued/importing/cancelling/removing, there is no safe worker to resume
    implicitly on the next launch. We expose the partial batch as interrupted
    so the user can remove it or explicitly re-import later.
    """
    db_path = await _ensure_db(workspace_path)
    now = _now_iso()
    async with aiosqlite.connect(str(db_path)) as db:
        active_placeholders = ",".join("?" for _ in _ACTIVE_STATES)
        cursor = await db.execute(
            f"""
            SELECT batch_id FROM source_import_batches
            WHERE state IN ({active_placeholders})
            """,
            list(_ACTIVE_STATES),
        )
        rows = await cursor.fetchall()
        batch_ids = [str(row[0]) for row in rows]
        if not batch_ids:
            return 0

        placeholders = ",".join("?" for _ in batch_ids)
        await db.execute(
            f"""
            UPDATE source_import_files
            SET status = 'failed',
                stage = 'interrupted',
                reason = 'app_closed_during_import',
                updated_at = ?
            WHERE batch_id IN ({placeholders})
              AND status = 'importing'
            """,
            [now, *batch_ids],
        )
        await db.execute(
            f"""
            UPDATE source_import_batches
            SET state = 'interrupted',
                current_file = NULL,
                updated_at = ?,
                finished_at = COALESCE(finished_at, ?)
            WHERE batch_id IN ({placeholders})
            """,
            [now, now, *batch_ids],
        )
        await db.commit()
    return len(batch_ids)


async def update_file_status(
    batch_id: str,
    file_id: str,
    status: SourceImportFileStatus,
    *,
    stage: Optional[str] = None,
    reason: Optional[str] = None,
    duplicate_of: Optional[str] = None,
    content_hash: Optional[str] = None,
    note_paths: Optional[list[str]] = None,
    workspace_path: Optional[Path] = None,
) -> None:
    db_path = await _ensure_db(workspace_path)
    now = _now_iso()
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            UPDATE source_import_files
            SET status = ?,
                stage = ?,
                reason = ?,
                duplicate_of = ?,
                content_hash = COALESCE(?, content_hash),
                note_paths = COALESCE(?, note_paths),
                updated_at = ?
            WHERE batch_id = ? AND file_id = ?
            """,
            (
                status,
                stage,
                reason,
                duplicate_of,
                content_hash,
                json.dumps(note_paths) if note_paths is not None else None,
                now,
                batch_id,
                file_id,
            ),
        )
        await db.commit()
